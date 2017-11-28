"""
A :class:`~allennlp.training.MultiTaskTrainer.MultiTaskTrainer` is responsible for training a
:class:`~allennlp.models.model.Model`.

Typically you might create a configuration file specifying the model and
training parameters and then use :mod:`~allennlp.commands.train`
rather than instantiating a ``MultiTaskTrainer`` yourself.
"""

import logging
import os
import pdb # pylint: disable=unused-import
import shutil
import time
import itertools
from typing import Dict, Optional, List

import torch
import torch.optim.lr_scheduler
from torch.nn.utils.clip_grad import clip_grad_norm
from torch.optim.lr_scheduler import _LRScheduler as PytorchLRScheduler  # pylint: disable=protected-access
import tqdm
from tensorboard import SummaryWriter

from allennlp.common import Params
from allennlp.common.checks import ConfigurationError
from allennlp.data.iterators.data_iterator import DataIterator
from allennlp.models.model import Model
from allennlp.nn.util import arrays_to_variables, device_mapping
from allennlp.training.learning_rate_schedulers import LearningRateScheduler
from allennlp.training.optimizers import Optimizer
logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


class MultiTaskTrainer:
    def __init__(self,
                 model: Model,
                 optimizer: torch.optim.Optimizer,
                 iterator: DataIterator,
                 patience: int = 2,
                 validation_metric: str = "-loss",
                 num_epochs: int = 20,
                 serialization_dir: Optional[str] = None,
                 cuda_device: int = -1,
                 grad_norm: Optional[float] = None,
                 grad_clipping: Optional[float] = None,
                 lr_decay: Optional[float] = None,
                 min_lr: Optional[float] = None,
                 learning_rate_scheduler: Optional[PytorchLRScheduler] = None,
                 no_tqdm: bool = False) -> None:
        """
        Parameters
        ----------
        model : ``Model``, required.
            An AllenNLP model to be optimized. Pytorch Modules can also be optimized if
            their ``forward`` method returns a dictionary with a "loss" key, containing a
            scalar tensor representing the loss function to be optimized.
        optimizer : ``torch.nn.Optimizer``, required.
            An instance of a Pytorch Optimizer, instantiated with the parameters of the
            model to be optimized.
        iterator : ``DataIterator``, required.
            A method for iterating over a ``Dataset``, yielding padded indexed batches.
        patience : int, optional (default=2)
            Number of epochs to be patient before early stopping.
        validation_metric : str, optional (default="loss")
            Validation metric to measure for whether to stop training using patience
            and whether to serialize an ``is_best`` model each epoch. The metric name
            must be prepended with either "+" or "-", which specifies whether the metric
            is an increasing or decreasing function.
        num_epochs : int, optional (default = 20)
            Number of training epochs.
        serialization_dir : str, optional (default=None)
            Path to directory for saving and loading model files. Models will not be saved if
            this parameter is not passed.
        cuda_device : int, optional (default = -1)
            An integer specifying the CUDA device to use. If -1, the CPU is used.
            Multi-gpu training is not currently supported, but will be once the
            Pytorch DataParallel API stabilises.
        grad_norm : float, optional, (default = None).
            If provided, gradient norms will be rescaled to have a maximum of this value.
        grad_clipping : ``float``, optional (default = ``None``).
            If provided, gradients will be clipped `during the backward pass` to have an (absolute)
            maximum of this value.  If you are getting ``NaNs`` in your gradients during training
            that are not solved by using ``grad_norm``, you may need this.
        learning_rate_scheduler : PytorchLRScheduler, optional, (default = None)
            A Pytorch learning rate scheduler. The learning rate will be decayed with respect to
            this schedule at the end of each epoch. If you use
            :class:`torch.optim.lr_scheduler.ReduceLROnPlateau`,
            this will use the ``validation_metric`` provided to determine if learning has plateaued.
        no_tqdm : ``bool``, optional (default=False)
            We use ``tqdm`` for logging, which will print a nice progress bar that updates in place
            after every batch.  This is nice if you're running training on a local shell, but can
            cause problems with log files from, e.g., a docker image running on kubernetes.  If
            ``no_tqdm`` is ``True``, we will not use tqdm, and instead log batch statistics using
            ``logger.info``, outputting a line at most every 10 seconds.
        """
        self._model = model
        self._iterator = iterator
        self._optimizer = optimizer

        self._patience = patience
        self._num_epochs = num_epochs
        self._serialization_dir = serialization_dir
        self._cuda_device = cuda_device
        self._grad_norm = grad_norm
        self._grad_clipping = grad_clipping
        self._learning_rate_scheduler = learning_rate_scheduler
        self._lr_decay = lr_decay
        self._min_lr = min_lr

        increase_or_decrease = validation_metric[0]
        if increase_or_decrease not in ["+", "-"]:
            raise ConfigurationError("Validation metrics must specify whether they should increase "
                                     "or decrease by pre-pending the metric name with a +/-.")
        self._validation_metric = validation_metric[1:]
        self._validation_metric_decreases = increase_or_decrease == "-"
        self._no_tqdm = no_tqdm

        if self._cuda_device >= 0:
            self._model = self._model.cuda(self._cuda_device)

        self._log_interval = 10  # seconds
        self._summary_interval = 100  # num batches between logging to tensorboard

    def train(self, tasks, batch_size, n_passes_per_epoch, load_model=1) -> None:
        '''
        Train on tasks.

        Args:
            - tasks (List[Task]):

        Returns: None
        '''

        epoch_counter = 0
        n_tasks = len(tasks)
        iterator = self._iterator(batch_size)

        # Resume from serialization path if it contains a saved model.
        if self._serialization_dir is not None:
            # Set up tensorboard logging.
            train_logs, val_logs = [], []
            for task in tasks:
                train_logs.append(SummaryWriter(os.path.join(
                    self._serialization_dir, task.name, "log", "train")))
                val_logs.append(SummaryWriter(os.path.join(
                    self._serialization_dir, task.name, "log", "valid")))
            if load_model and \
                    any(["model_state_epoch_" in x
                         for x in os.listdir(self._serialization_dir)]):
                logger.info("Loading model from checkpoint.")
                epoch_counter = self._restore_checkpoint()

        if self._grad_clipping is not None:
            # pylint: disable=invalid-unary-operand-type
            clip_function = lambda grad: grad.clamp(
                -self._grad_clipping, self._grad_clipping)
            for parameter in self._model.parameters():
                if parameter.requires_grad:
                    parameter.register_hook(clip_function)

        logger.info("Beginning training.")
        task_infos = [{} for _ in range(n_tasks)]
        for task_idx, task in enumerate(tasks):
            n_tr_batches = iterator.get_num_batches(task.train_data)
            n_val_batches = iterator.get_num_batches(task.val_data)
            task_infos[task_idx]['n_tr_batches'] = n_tr_batches
            task_infos[task_idx]['n_val_batches'] = n_val_batches
            task_infos[task_idx]['n_batches_per_pass'] = \
                    int(n_tr_batches / n_passes_per_epoch)

        validation_metric_per_epoch: List[float] = []

        for epoch in range(epoch_counter, self._num_epochs):
            logger.info("*** Epoch %d/%d ***", epoch + 1, self._num_epochs)
            logger.info("learning rate: %.5f",
                        self._optimizer.param_groups[0]['lr'])
            all_tr_metrics = {}

            '''
            for task_info in task_infos:
                if 'tr_generator' in task_info:
                    del task_info['tr_generator']
                if 'val_generator' in task_info:
                    del task_info['val_generator']
            '''

            for task_idx, task in enumerate(tasks):
                iterator = self._iterator(batch_size)
                tr_generator = tqdm.tqdm(iterator(task.train_data, num_epochs=1),
                                         disable=self._no_tqdm,
                                         total=task_infos[task_idx]['n_tr_batches'])
                val_generator = tqdm.tqdm(iterator(task.val_data, num_epochs=1),
                                          disable=self._no_tqdm,
                                          total=task_infos[task_idx]['n_val_batches'])
                task_infos[task_idx]['tr_generator'] = tr_generator
                task_infos[task_idx]['val_generator'] = val_generator
                task_infos[task_idx]['loss'] = 0.0
                task_infos[task_idx]['n_batches_trained'] = 0

            self._model.train()
            for _ in range(n_passes_per_epoch):
                task_order = range(len(tasks)) # some ordering
                for task_idx in task_order:
                    task = tasks[task_idx]
                    tr_generator = task_infos[task_idx]['tr_generator']
                    n_tr_batches = task_infos[task_idx]['n_tr_batches']
                    n_batches_to_train = task_infos[task_idx]['n_batches_per_pass']
                    n_batches_trained = task_infos[task_idx]['n_batches_trained']
                    tr_loss = task_infos[task_idx]['loss']
                    logger.info("\tTraining %s task (%d/%d)",
                                task.name, task_idx + 1, len(tasks))
                    last_log = time.time()
                    for batch in itertools.islice(tr_generator, n_batches_to_train):
                        n_batches_trained += 1
                        self._optimizer.zero_grad()
                        output_dict = self._forward(batch, task=task,
                                                    for_training=True)
                        try:
                            loss = output_dict["loss"]
                            loss.backward()
                            tr_loss += loss.data.cpu().numpy()
                        except KeyError:
                            raise ConfigurationError("The model you are trying to"
                                                     + "optimize does not contain a"
                                                     + " 'loss' key in the output "
                                                     + "of model.forward(inputs).")
                        except:
                            pdb.set_trace()

                        # Gradient regularization and application
                        if self._grad_norm:
                            clip_grad_norm(self._model.parameters(),
                                           self._grad_norm)
                        self._optimizer.step()

                        # Training logging
                        task_metrics = task.get_metrics()
                        task_metrics["%s_loss" % task.name] = \
                                float(tr_loss / n_batches_trained)
                        description = self._description_from_metrics(task_metrics)
                        tr_generator.set_description(description)

                        # Tensorboard logging
                        batch_num_total = n_tr_batches * epoch + n_batches_trained
                        if self._serialization_dir and \
                                batch_num_total % self._summary_interval == 0:
                            for name, param in self._model.named_parameters():
                                train_logs[task_idx].add_scalar("PARAMETER_MEAN/" +\
                                        name, param.data.mean(), batch_num_total)
                                train_logs[task_idx].add_scalar("PARAMETER_STD/" + \
                                        name, param.data.std(), batch_num_total)
                                if param.grad is not None:
                                    train_logs[task_idx].add_scalar("GRAD_MEAN/" + \
                                            name, param.grad.data.mean(), \
                                            batch_num_total)
                                    train_logs[task_idx].add_scalar("GRAD_STD/" + \
                                            name, param.grad.data.std(), \
                                            batch_num_total)
                            train_logs[task_idx].add_scalar("LOSS/loss_train", \
                                    task_metrics["%s_loss" % task.name], \
                                    batch_num_total)

                        # More training logging
                        if self._no_tqdm and time.time() - last_log > self._log_interval:
                            logger.info("Batch %d/%d: %s", n_batches_trained,
                                        n_tr_batches, description)
                            for name, param in self._model.named_parameters():
                                if param.grad is None:
                                    continue
                                logger.debug("GRAD MEAN %s: %.7f", name,
                                             param.grad.data.mean())
                                logger.debug("GRAD STD %s: %.7f", name, \
                                             param.grad.data.std())
                            last_log = time.time()

                        # Update training progress on that task
                        task_infos[task_idx]['n_batches_trained'] = n_batches_trained
                        task_infos[task_idx]['loss'] = tr_loss

            # Overall training logging
            for task, task_info in zip(tasks, task_infos):
                try:
                    task_metrics = task.get_metrics(reset=True)
                    for name, value in task_metrics.items():
                        all_tr_metrics["%s_%s" % (task.name, name)] = value
                        all_tr_metrics["%s_loss" % task.name] = \
                                float(tr_loss / task_info['n_batches_trained'])
                except Exception as e:
                    print(e)
                    pdb.set_trace()


            # TODO(Alex): make sure trained on all batches for each task

            # Validation
            logger.info("Validating...")
            val_losses = [0.0] * n_tasks # MAYBE A PROBLEM
            all_val_metrics = {}
            self._model.eval()
            for task_idx, task in enumerate(tasks):
                n_val_batches = task_infos[task_idx]['n_val_batches']
                val_generator = iterator(task.val_data, num_epochs=1)
                val_generator_tqdm = tqdm.tqdm(val_generator,
                                               disable=self._no_tqdm,
                                               total=n_val_batches)
                batch_num = 0
                for batch in val_generator_tqdm:
                    batch_num += 1
                    val_output_dict = self._forward(batch, task=task,
                                                    for_training=False)
                    loss = val_output_dict["loss"]
                    val_losses[task_idx] += loss.data.cpu().numpy()
                    task_metrics = task.get_metrics()
                    task_metrics["%s_loss" % task.name] = \
                            float(val_losses[task_idx] / batch_num)
                    description = self._description_from_metrics(task_metrics)
                    val_generator_tqdm.set_description(description)
                    if self._no_tqdm and \
                            time.time() - last_log > self._log_interval:
                        logger.info("Batch %d/%d: %s", batch_num, \
                                    n_val_batches, description)
                        last_log = time.time()

                task_metrics = task.get_metrics(reset=True)
                for name, value in task_metrics.items():
                    all_val_metrics["%s_%s" % (task.name, name)] = value
                all_val_metrics["%s_loss" % task.name] = \
                        float(val_losses[task_idx] / batch_num)

            for name, value in all_tr_metrics.items():
                logger.info("Statistic: %s", name)
                logger.info("\ttraining: %3f", value)
                logger.info("\tvalidation: %3f", all_val_metrics[name])
                if self._serialization_dir:
                    train_logs[task_idx].add_scalar(name, value, epoch)
                    val_logs[task_idx].add_scalar(name, all_val_metrics[name],
                                                  epoch)

            this_epoch_val_metric = all_val_metrics[self._validation_metric]
            if len(validation_metric_per_epoch) > self._patience:
                # Is the worst validation performance in past self._patience
                # epochs is better than current value?
                if self._validation_metric_decreases:
                    should_stop = max(
                        validation_metric_per_epoch[-self._patience:]) < \
                        this_epoch_val_metric
                else:
                    should_stop = min(
                        validation_metric_per_epoch[-self._patience:]) > \
                        this_epoch_val_metric
                if should_stop:
                    logger.info("Ran out of patience.  Stopping training.")
                    return # can't break because of task loop
            validation_metric_per_epoch.append(this_epoch_val_metric)

            if self._validation_metric_decreases:
                is_best_so_far = this_epoch_val_metric == \
                        min(validation_metric_per_epoch)
            else:
                is_best_so_far = this_epoch_val_metric == \
                        max(validation_metric_per_epoch)
            if self._serialization_dir:
                self._save_checkpoint(epoch, is_best=is_best_so_far)

            if self._learning_rate_scheduler:
                # Grim hack to determine whether the validation metric we
                # are recording needs to be passed to the scheduler.
                # This is required because the step() function of the
                # different schedulers are (understandably) different to
                # ReduceLROnPlateau.
                if isinstance(self._learning_rate_scheduler,
                              torch.optim.lr_scheduler.ReduceLROnPlateau):
                    self._learning_rate_scheduler.step(this_epoch_val_metric,
                                                       epoch)
                else:
                    self._learning_rate_scheduler.step(epoch)

            if not is_best_so_far and self._lr_decay:
                self._optimizer.param_groups[0]['lr'] *= self._lr_decay
            if self._optimizer.param_groups[0]['lr'] < self._min_lr:
                logger.info("Minimum lr hit. Stopping training.")
                return

    def _forward(self, batch: dict, for_training: bool,
                 #pred_layer, pair_input=1, scorer=None) -> dict:
                 task=None) -> dict:
        tensor_batch = arrays_to_variables(batch, self._cuda_device, for_training=for_training)
        #return self._model.forward(pred_layer, pair_input, scorer, **tensor_batch)
        return self._model.forward(task, **tensor_batch)

    def _description_from_metrics(self, metrics: Dict[str, float]) -> str:
        # pylint: disable=no-self-use
        return ', '.join(["%s: %.4f" % (name, value) for name, value in metrics.items()]) + " ||"

    def _save_checkpoint(self,
                         epoch: int,
                         is_best: Optional[bool] = None) -> None:
        """
        Parameters
        ----------
        epoch : int, required.
            The epoch of training.
        is_best: bool, optional (default = None)
            A flag which causes the model weights at the given epoch to
            be copied to a "best.th" file. The value of this flag should
            be based on some validation metric computed by your model.
        """
        model_path = os.path.join(self._serialization_dir, "model_state_epoch_{}.th".format(epoch))
        model_state = self._model.state_dict()
        torch.save(model_state, model_path)

        training_state = {'epoch': epoch, 'optimizer': self._optimizer.state_dict()}
        torch.save(training_state, os.path.join(self._serialization_dir,
                                                "training_state_epoch_{}.th".format(epoch)))
        if is_best:
            logger.info("Best validation performance so far. "
                        "Copying weights to %s/best.th'.", self._serialization_dir)
            shutil.copyfile(model_path, os.path.join(self._serialization_dir, "best.th"))

    def _restore_checkpoint(self) -> int:
        """
        Restores a model from a serialization_dir to the last saved checkpoint.
        This includes an epoch count and optimizer state, which is serialized separately
        from  model parameters. This function should only be used to continue training -
        if you wish to load a model for inference/load parts of a model into a new
        computation graph, you should use the native Pytorch functions:
        `` model.load_state_dict(torch.load("/path/to/model/weights.th"))``

        Returns
        -------
        epoch: int
            The epoch at which to resume training.
        """
        if not self._serialization_dir:
            raise ConfigurationError("serialization_dir not specified - cannot "
                                     "restore a model without a directory path.")

        serialization_files = os.listdir(self._serialization_dir)
        model_checkpoints = [x for x in serialization_files if "model_state_epoch" in x]
        epoch_to_load = max([int(x.split("model_state_epoch_")[-1].strip(".th")) \
                             for x in model_checkpoints])

        model_path = os.path.join(self._serialization_dir,
                                  "model_state_epoch_{}.th".format(epoch_to_load))
        training_state_path = os.path.join(self._serialization_dir,
                                           "training_state_epoch_{}.th".format(epoch_to_load))

        model_state = torch.load(model_path, map_location=device_mapping(self._cuda_device))
        training_state = torch.load(training_state_path)
        self._model.load_state_dict(model_state)
        self._optimizer.load_state_dict(training_state["optimizer"])
        return training_state["epoch"]

    @classmethod
    def from_params(cls,
                    model: Model,
                    serialization_dir: str,
                    iterator: DataIterator,
                    params: Params) -> 'MultiTaskTrainer':
        '''
        Generator trainer from parameters.
        '''

        patience = params.pop("patience", 2)
        validation_metric = params.pop("validation_metric", "-loss")
        num_epochs = params.pop("num_epochs", 20)
        cuda_device = params.pop("cuda_device", -1)
        grad_norm = params.pop("grad_norm", None)
        grad_clipping = params.pop("grad_clipping", None)
        lr_decay = params.pop("lr_decay", None)
        min_lr = params.pop("min_lr", None)
        lr_scheduler_params = params.pop("learning_rate_scheduler", None)

        if cuda_device >= 0:
            model = model.cuda(cuda_device)
        parameters = [p for p in model.parameters() if p.requires_grad]
        optimizer = Optimizer.from_params(parameters, params.pop("optimizer"))

        if lr_scheduler_params:
            scheduler = LearningRateScheduler.from_params(optimizer, lr_scheduler_params)
        else:
            scheduler = None
        no_tqdm = params.pop("no_tqdm", False)

        params.assert_empty(cls.__name__)
        return MultiTaskTrainer(model, optimizer, iterator,
                                patience=patience,
                                validation_metric=validation_metric,
                                num_epochs=num_epochs,
                                serialization_dir=serialization_dir,
                                cuda_device=cuda_device,
                                grad_norm=grad_norm,
                                grad_clipping=grad_clipping,
                                lr_decay=lr_decay,
                                min_lr=min_lr,
                                learning_rate_scheduler=scheduler,
                                no_tqdm=no_tqdm)