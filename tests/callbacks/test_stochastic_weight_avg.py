# Copyright The PyTorch Lightning team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import logging
import os
from pathlib import Path
from typing import Dict, Optional
from unittest import mock

import pytest
import torch
from torch import nn
from torch.optim.swa_utils import SWALR
from torch.utils.data import DataLoader

from pytorch_lightning import LightningModule, Trainer
from pytorch_lightning.callbacks import ModelCheckpoint, StochasticWeightAveraging
from pytorch_lightning.core.datamodule import LightningDataModule
from pytorch_lightning.plugins import DDPSpawnPlugin
from pytorch_lightning.plugins.training_type import TrainingTypePlugin
from pytorch_lightning.utilities.exceptions import MisconfigurationException
from tests.helpers.boring_model import BoringModel, RandomDataset, RandomIterableDataset
from tests.helpers.runif import RunIf


class SwaTestModel(BoringModel):
    def __init__(
        self, batchnorm: bool = True, interval: str = "epoch", iterable_dataset: bool = False, crash_after_epoch=None
    ):
        super().__init__()
        layers = [nn.Linear(32, 32)]
        if batchnorm:
            layers.append(nn.BatchNorm1d(32))
        layers += [nn.ReLU(), nn.Linear(32, 2)]
        self.layer = nn.Sequential(*layers)
        self.interval = interval
        self.iterable_dataset = iterable_dataset
        self.crash_after_epoch = crash_after_epoch
        self._epoch_count = 0
        self.save_hyperparameters()

    def training_step(self, batch, batch_idx):
        output = self.forward(batch)
        loss = self.loss(batch, output)
        return {"loss": loss}

    def validation_step(self, batch, batch_idx):
        output = self.forward(batch)
        loss = self.loss(batch, output)
        self.log("val_loss", loss)
        return {"x": loss}

    def train_dataloader(self):

        dset_cls = RandomIterableDataset if self.iterable_dataset else RandomDataset
        dset = dset_cls(32, 64)

        return DataLoader(dset, batch_size=2)

    def val_dataloader(self):
        return self.train_dataloader()

    def configure_optimizers(self):
        optimizer = torch.optim.SGD(self.layer.parameters(), lr=0.1)
        return {
            "optimizer": optimizer,
            "lr_scheduler": {
                "scheduler": torch.optim.lr_scheduler.StepLR(optimizer, step_size=1),
                "interval": self.interval,
            },
        }

    def training_epoch_end(self, _):
        if not self.crash_after_epoch:
            return
        self._epoch_count += 1
        if self._epoch_count >= self.crash_after_epoch:
            raise RuntimeError("Crash test")


class SwaTestCallback(StochasticWeightAveraging):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.validation_calls: int = 0
        self.update_parameters_calls: int = 0
        self.transfer_weights_calls: int = 0
        self.weight_source_map: Dict[LightningModule, LightningModule] = {}
        # Record the first epoch, as if we are resuming from a checkpoint this may not be equal to 0
        self.first_epoch: Optional[int] = None

    def update_parameters(self, *args, **kwargs):
        self.update_parameters_calls += 1
        return StochasticWeightAveraging.update_parameters(*args, **kwargs)

    def on_validation_start(self, trainer: Trainer, pl_module: LightningModule):
        self.validation_calls += 1
        super().on_validation_start(trainer, pl_module)
        within_swa_epoch = self.swa_start <= trainer.current_epoch <= self.swa_end
        self._verify_swa_weights_used(pl_module, self._swa_validation and within_swa_epoch)
        if within_swa_epoch:
            # Either swa_validation is disabled and batch norm batch counts should not be zeroed
            # as weights aren't transferred, or the batch norm update should have been applied.
            expect_bn_update = self._validation_batch_norm_update or not self._swa_validation
            self._verify_batch_norm_updated(pl_module, expect_bn_update)

    def on_validation_end(self, trainer: Trainer, pl_module: LightningModule):
        super().on_validation_end(trainer, pl_module)
        self._verify_swa_weights_used(pl_module, False)

    def transfer_weights(self, src_pl_module: LightningModule, dst_pl_module: LightningModule):
        self.transfer_weights_calls += 1
        # Track where module weights have been transferred from so that we can verify whether we are using SWA weights
        self.weight_source_map[dst_pl_module] = self.weight_source_map.get(src_pl_module, src_pl_module)
        # After transferring weights, batch norm moments are no longer up to date. We reset the batch norm batch count
        # to zero so that we can later test whether the batch norm parameters have been updated.
        self._zero_batch_norm_batch_count(dst_pl_module)
        return StochasticWeightAveraging.transfer_weights(src_pl_module, dst_pl_module)

    def on_train_epoch_start(self, trainer, *args):
        super().on_train_epoch_start(trainer, *args)
        if self.first_epoch is None:
            self.first_epoch = trainer.current_epoch
        if self.swa_start <= trainer.current_epoch:
            assert isinstance(trainer.lr_schedulers[0]["scheduler"], SWALR)
            assert trainer.lr_schedulers[0]["interval"] == "epoch"
            assert trainer.lr_schedulers[0]["frequency"] == 1

    def on_train_epoch_end(self, trainer, *args):
        super().on_train_epoch_end(trainer, *args)
        if self.swa_start <= trainer.current_epoch <= self.swa_end:
            swa_epoch = trainer.current_epoch - self.swa_start
            assert self.n_averaged == swa_epoch + 1
            assert self._swa_scheduler is not None
            # Scheduler is stepped once on initialization and then at the end of each epoch
            assert self._swa_scheduler._step_count == swa_epoch + 2
        elif trainer.current_epoch > self.swa_end:
            assert self.n_averaged == self._max_epochs - self.swa_start

    def on_train_end(self, trainer, pl_module):
        super().on_train_end(trainer, pl_module)

        if not isinstance(trainer.training_type_plugin, DDPSpawnPlugin):
            # check backward call count. the batchnorm update epoch should not backward
            assert trainer.training_type_plugin.backward.call_count == (
                (trainer.max_epochs - self.first_epoch) * trainer.limit_train_batches
            )

        # check call counts
        first_swa_epoch = max(self.first_epoch, self.swa_start)
        assert self.update_parameters_calls == trainer.max_epochs - first_swa_epoch
        if self._swa_validation:
            # 3 weight transfers are needed per SWA validation step
            assert self.transfer_weights_calls == (self.validation_calls - self.swa_start) * 3 + 1
        else:
            assert self.transfer_weights_calls == 1

        # check average parameters have been transferred
        self._verify_swa_weights_used(pl_module, True)
        self._verify_batch_norm_updated(pl_module)

    def _verify_swa_weights_used(self, pl_module: LightningModule, expect_weights_used: bool):
        """Test whether the provided module is using SWA parameters."""
        if expect_weights_used:
            # Weights should originate from the averaged model
            assert self.weight_source_map.get(pl_module) is self._average_model
        else:
            # Weights should originate from the module itself
            assert self.weight_source_map.get(pl_module, pl_module) is pl_module

    @staticmethod
    def _zero_batch_norm_batch_count(pl_module: LightningModule):
        for module in pl_module.modules():
            if isinstance(module, nn.modules.batchnorm._BatchNorm):
                module.num_batches_tracked *= 0

    @staticmethod
    def _verify_batch_norm_updated(pl_module: LightningModule, expect_updated: bool = True):
        for module in pl_module.modules():
            if isinstance(module, nn.modules.batchnorm._BatchNorm):
                if expect_updated:
                    assert module.num_batches_tracked.item() > 0
                else:
                    assert module.num_batches_tracked.item() == 0


class SwaTestDataModule(LightningDataModule):
    """Shim data module that just wraps a model."""

    def __init__(self, model: LightningModule):
        super().__init__()
        self._model = model

    def train_dataloader(self):
        return self._model.train_dataloader()

    def test_dataloader(self):
        return self._model.test_dataloader()

    def predict_dataloader(self):
        return self._model.predict_dataloader()

    def val_dataloader(self):
        return self._model.val_dataloader()


def train_with_swa(
    tmpdir,
    batchnorm=True,
    strategy=None,
    gpus=None,
    num_processes=1,
    interval="epoch",
    iterable_dataset=False,
    validation=False,
    validation_batchnorm_update=True,
):
    model = SwaTestModel(batchnorm=batchnorm, interval=interval, iterable_dataset=iterable_dataset)
    swa_start = 2
    max_epochs = 5
    swa_callback = SwaTestCallback(
        swa_epoch_start=swa_start,
        swa_lrs=0.1,
        swa_validation=validation,
        validation_batch_norm_update=validation_batchnorm_update,
    )
    assert swa_callback.update_parameters_calls == 0
    assert swa_callback.transfer_weights_calls == 0

    trainer = Trainer(
        default_root_dir=tmpdir,
        enable_progress_bar=False,
        max_epochs=max_epochs,
        limit_train_batches=5,
        limit_val_batches=5,
        num_sanity_val_steps=0,
        callbacks=[swa_callback],
        accumulate_grad_batches=2,
        strategy=strategy,
        gpus=gpus,
        num_processes=num_processes,
    )

    with mock.patch.object(TrainingTypePlugin, "backward", wraps=trainer.training_type_plugin.backward):
        trainer.fit(model)

    # check the model is the expected
    assert trainer.lightning_module == model


@RunIf(min_gpus=2, standalone=True)
def test_swa_callback_ddp(tmpdir):
    train_with_swa(tmpdir, strategy="ddp", gpus=2)


@RunIf(min_gpus=2)
def test_swa_callback_ddp_spawn(tmpdir):
    train_with_swa(tmpdir, strategy="ddp_spawn", gpus=2)


@RunIf(skip_windows=True, skip_49370=True)
def test_swa_callback_ddp_cpu(tmpdir):
    train_with_swa(tmpdir, strategy="ddp_spawn", num_processes=2)


@RunIf(min_gpus=1)
def test_swa_callback_1_gpu(tmpdir):
    train_with_swa(tmpdir, gpus=1)


@pytest.mark.parametrize("batchnorm", (True, False))
@pytest.mark.parametrize("iterable_dataset", (True, False))
@pytest.mark.parametrize("validation", (True, False))
def test_swa_callback(tmpdir, batchnorm: bool, iterable_dataset: bool, validation: bool):
    train_with_swa(tmpdir, batchnorm=batchnorm, iterable_dataset=iterable_dataset, validation=validation)


@pytest.mark.parametrize("validation", (True, False))
def test_swa_callback_without_validation_batchnorm_update(tmpdir, validation):
    train_with_swa(tmpdir, batchnorm=True, validation=validation, validation_batchnorm_update=False)


@pytest.mark.parametrize("interval", ("epoch", "step"))
def test_swa_callback_scheduler_step(tmpdir, interval: str):
    train_with_swa(tmpdir, interval=interval)


def test_swa_warns(tmpdir, caplog):
    model = SwaTestModel(interval="step")
    trainer = Trainer(default_root_dir=tmpdir, fast_dev_run=True, callbacks=StochasticWeightAveraging())
    with caplog.at_level(level=logging.INFO), pytest.warns(UserWarning, match="SWA is currently only supported"):
        trainer.fit(model)
    assert "Swapping scheduler `StepLR` for `SWALR`" in caplog.text


def test_swa_raises():
    with pytest.raises(MisconfigurationException, match=">0 integer or a float between 0 and 1"):
        StochasticWeightAveraging(swa_epoch_start=0, swa_lrs=0.1)
    with pytest.raises(MisconfigurationException, match=">0 integer or a float between 0 and 1"):
        StochasticWeightAveraging(swa_epoch_start=1.5, swa_lrs=0.1)
    with pytest.raises(MisconfigurationException, match=">0 integer or a float between 0 and 1"):
        StochasticWeightAveraging(swa_epoch_start=-1, swa_lrs=0.1)
    with pytest.raises(MisconfigurationException, match="positive float, or a list of positive floats"):
        StochasticWeightAveraging(swa_epoch_start=5, swa_lrs=[0.2, 1])


@pytest.mark.parametrize("stochastic_weight_avg", [False, True])
@pytest.mark.parametrize("use_callbacks", [False, True])
def test_trainer_and_stochastic_weight_avg(tmpdir, use_callbacks: bool, stochastic_weight_avg: bool):
    """Test to ensure SWA Callback is injected when `stochastic_weight_avg` is provided to the Trainer."""

    class TestModel(BoringModel):
        def configure_optimizers(self):
            optimizer = torch.optim.SGD(self.layer.parameters(), lr=0.1)
            return optimizer

    model = TestModel()
    kwargs = {
        "default_root_dir": tmpdir,
        "callbacks": StochasticWeightAveraging(swa_lrs=1e-3) if use_callbacks else None,
        "stochastic_weight_avg": stochastic_weight_avg,
        "limit_train_batches": 4,
        "limit_val_batches": 4,
        "max_epochs": 2,
    }
    if stochastic_weight_avg:
        with pytest.deprecated_call(match=r"stochastic_weight_avg=True\)` is deprecated in v1.5"):
            trainer = Trainer(**kwargs)
    else:
        trainer = Trainer(**kwargs)
    trainer.fit(model)
    if use_callbacks or stochastic_weight_avg:
        assert sum(1 for cb in trainer.callbacks if isinstance(cb, StochasticWeightAveraging)) == 1
        assert trainer.callbacks[0]._swa_lrs == [1e-3 if use_callbacks else 0.1]
    else:
        assert all(not isinstance(cb, StochasticWeightAveraging) for cb in trainer.callbacks)


def test_swa_deepcopy(tmpdir):
    """Test to ensure SWA Callback doesn't deepcopy dataloaders and datamodule potentially leading to OOM."""

    class TestSWA(StochasticWeightAveraging):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.on_before_accelerator_backend_setup_called = False

        def on_before_accelerator_backend_setup(self, trainer: "Trainer", pl_module: "LightningModule"):
            super().on_before_accelerator_backend_setup(trainer, pl_module)
            assert self._average_model.train_dataloader is not pl_module.train_dataloader
            assert self._average_model.train_dataloader.__self__ == self._average_model
            assert self._average_model.trainer is None
            self.on_before_accelerator_backend_setup_called = True

    model = BoringModel()
    swa = TestSWA()
    trainer = Trainer(default_root_dir=tmpdir, callbacks=swa, fast_dev_run=True)
    trainer.fit(model, train_dataloaders=DataLoader(RandomDataset(32, 2)))
    assert swa.on_before_accelerator_backend_setup_called


def test_swa_multiple_lrs(tmpdir):
    swa_lrs = [0.123, 0.321]

    class TestModel(BoringModel):
        def __init__(self):
            super().__init__()
            self.layer1 = torch.nn.Linear(32, 32)
            self.layer2 = torch.nn.Linear(32, 2)
            self.on_train_epoch_start_called = False

        def forward(self, x):
            x = self.layer1(x)
            x = self.layer2(x)
            return x

        def configure_optimizers(self):
            params = [{"params": self.layer1.parameters(), "lr": 0.1}, {"params": self.layer2.parameters(), "lr": 0.2}]
            return torch.optim.Adam(params)

        def on_train_epoch_start(self):
            optimizer = trainer.optimizers[0]
            assert [pg["lr"] for pg in optimizer.param_groups] == [0.1, 0.2]
            assert [pg["initial_lr"] for pg in optimizer.param_groups] == swa_lrs
            assert [pg["swa_lr"] for pg in optimizer.param_groups] == swa_lrs
            self.on_train_epoch_start_called = True

    model = TestModel()
    swa_callback = StochasticWeightAveraging(swa_lrs=swa_lrs)
    trainer = Trainer(
        default_root_dir=tmpdir,
        callbacks=swa_callback,
        fast_dev_run=1,
    )
    trainer.fit(model)
    assert model.on_train_epoch_start_called


def swa_resume_training_from_checkpoint(tmpdir, crash_after_epoch=4, ddp=False):
    model = SwaTestModel(crash_after_epoch=crash_after_epoch)
    swa_start = 3
    max_epochs = 5
    swa_callback = SwaTestCallback(swa_epoch_start=swa_start, swa_lrs=0.1)

    num_processes = 2 if ddp else 1
    strategy = "ddp_spawn" if ddp else None

    trainer = Trainer(
        default_root_dir=tmpdir,
        enable_progress_bar=False,
        max_epochs=max_epochs,
        limit_train_batches=5,
        limit_val_batches=0,
        callbacks=[swa_callback],
        accumulate_grad_batches=2,
        num_processes=num_processes,
        strategy=strategy,
    )

    exception_type = Exception if ddp else RuntimeError
    backward_patch = mock.patch.object(TrainingTypePlugin, "backward", wraps=trainer.training_type_plugin.backward)
    with backward_patch, pytest.raises(exception_type):
        trainer.fit(model)

    checkpoint_dir = Path(tmpdir) / "lightning_logs" / "version_0" / "checkpoints"
    checkpoint_files = os.listdir(checkpoint_dir)
    assert len(checkpoint_files) == 1
    checkpoint_path = checkpoint_dir / checkpoint_files[0]

    model = SwaTestModel()
    swa_callback = SwaTestCallback(swa_epoch_start=swa_start, swa_lrs=0.1)
    trainer = Trainer(
        default_root_dir=tmpdir,
        enable_progress_bar=False,
        max_epochs=max_epochs,
        limit_train_batches=5,
        limit_val_batches=0,
        callbacks=[swa_callback],
        accumulate_grad_batches=2,
        num_processes=num_processes,
        strategy=strategy,
    )

    with mock.patch.object(TrainingTypePlugin, "backward", wraps=trainer.training_type_plugin.backward):
        trainer.fit(model, ckpt_path=checkpoint_path.as_posix())


@pytest.mark.parametrize("crash_after_epoch", [2, 4])
def test_swa_resume_training_from_checkpoint(tmpdir, crash_after_epoch):
    swa_resume_training_from_checkpoint(tmpdir, crash_after_epoch=crash_after_epoch)


@RunIf(skip_windows=True, min_torch="1.8")
def test_swa_resume_training_from_checkpoint_ddp(tmpdir):
    # Requires PyTorch >= 1.8 to include this segfault fix:
    # https://github.com/pytorch/pytorch/pull/50998
    swa_resume_training_from_checkpoint(tmpdir, ddp=True)


def _test_misconfiguration_error_with_sharded_model(tmpdir, strategy, gpus=None):
    model = SwaTestModel()
    swa_callback = SwaTestCallback(swa_epoch_start=2, swa_lrs=0.1)
    trainer = Trainer(
        default_root_dir=tmpdir,
        enable_progress_bar=False,
        max_epochs=5,
        callbacks=[swa_callback],
        strategy=strategy,
        gpus=gpus,
    )
    with pytest.raises(MisconfigurationException, match="SWA does not currently support sharded models"):
        trainer.fit(model)


@RunIf(fairscale_fully_sharded=True, min_gpus=1)
def test_misconfiguration_error_with_ddp_fully_sharded(tmpdir):
    _test_misconfiguration_error_with_sharded_model(tmpdir, "fsdp", 1)


@RunIf(deepspeed=True)
def test_misconfiguration_error_with_deep_speed(tmpdir):
    _test_misconfiguration_error_with_sharded_model(tmpdir, "deepspeed")


@pytest.mark.parametrize("batchnorm", (True, False))
@pytest.mark.parametrize("within_swa_epochs", (True, False))
@pytest.mark.parametrize("use_datamodule", (True, False))
def test_swa_load_best_checkpoint(tmpdir, batchnorm: bool, within_swa_epochs: bool, use_datamodule: bool):
    model = SwaTestModel(batchnorm=batchnorm)
    if within_swa_epochs:
        # Start at epoch 1, so we can guarantee the best checkpoint should be saved with SWA weights
        swa_start = 1
    else:
        # Start after the last epoch, so we never save a checkpoint with SWA parameters
        swa_start = 6
    max_epochs = 5

    swa_callback = SwaTestCallback(swa_epoch_start=swa_start, swa_lrs=0.1, swa_validation=True)
    checkpoint_callback = ModelCheckpoint(monitor="val_loss", save_top_k=3, mode="min")

    trainer = Trainer(
        default_root_dir=tmpdir,
        enable_progress_bar=False,
        max_epochs=max_epochs,
        limit_train_batches=5,
        limit_val_batches=5,
        num_sanity_val_steps=0,
        callbacks=[swa_callback, checkpoint_callback],
        accumulate_grad_batches=2,
        num_processes=1,
    )

    with mock.patch.object(TrainingTypePlugin, "backward", wraps=trainer.training_type_plugin.backward):
        trainer.fit(model)

    datamodule = SwaTestDataModule(model) if use_datamodule else None

    checkpoint_path = checkpoint_callback.best_model_path
    new_model = SwaTestModel.load_from_checkpoint(checkpoint_path)
    parameters_loaded = SwaTestCallback.restore_average_parameters_from_checkpoint(
        new_model, checkpoint_path, datamodule=datamodule
    )

    assert parameters_loaded == within_swa_epochs
