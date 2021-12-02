# Copyright (c) 2020 Graphcore Ltd. All rights reserved.
from tqdm import tqdm
import os
import math
import time
import torch
import poptorch
import logging
import popdist
import horovod.torch as hvd
import numpy as np

from poptorch.optim import SGD, RMSprop, AdamW, LAMB
from lr_schedule import WarmUpLRDecorator, PeriodicLRDecorator
from torch.optim.lr_scheduler import MultiStepLR, CosineAnnealingLR, ExponentialLR
from train_utils import parse_arguments
from validate import test, validate_checkpoints, create_validation_model_opts
import weight_avg
import sys
import import_helper
import models
import models.loss
import utils
import datasets
import datasets.augmentations as augmentations


class TrainingModelWithLoss(torch.nn.Module):
    def __init__(self, model, label_smoothing=0.0, use_mixup=False, use_cutmix=False):
        super().__init__()
        self.model = model
        self.label_smoothing = label_smoothing
        self.use_mixup = use_mixup
        self.use_cutmix = use_cutmix
        if self.use_mixup or self.use_cutmix:
            self.loss = models.loss.weighted_nll_loss
        else:
            self.loss = torch.nn.NLLLoss(reduction='mean')
        # Use human readable names for each layer
        models.NameScopeHook(self)

    def forward(self, input_data, labels=None):
        if self.use_mixup or self.use_cutmix:
            output, coeffs = self.model(input_data)
        else:
            output = self.model(input_data)
        # Calculate loss in full precision
        output = output.float()
        if labels is None:
            return output

        loss_items = {}
        log_preds = torch.nn.functional.log_softmax(output, dim=1)

        if self.use_mixup or self.use_cutmix:
            all_labels, weights = self.model.mix_labels(labels, coeffs)
            classification_loss = self.loss(log_preds, all_labels, weights)
        else:
            classification_loss = self.loss(log_preds, labels)
        loss_items['classification_loss'] = (1.0 - self.label_smoothing) * classification_loss

        if self.label_smoothing > 0.0:
            # cross entropy between uniform distribution and output distribution
            loss_items["smoothing_loss"] = -torch.mean(log_preds) * self.label_smoothing
            final_loss = loss_items["smoothing_loss"] + loss_items["classification_loss"]
        else:
            final_loss = loss_items["classification_loss"]

        with torch.no_grad():
            acc = utils.accuracy(output, labels)
        return acc, poptorch.identity_loss(final_loss, reduction='none'), tuple(loss_items.values())


def train(training_model, training_data, opts, lr_scheduler, epochs, optimizer, validation_function=None):
    old_lr = lr_scheduler.get_last_lr()[0]
    iterations_per_epoch = len(training_data)
    num_instances = opts.popdist_size if opts.use_popdist else 1
    metrics = utils.Metrics(running_mean_length=iterations_per_epoch, distributed=opts.use_popdist)
    # Determine loss scaling change points
    num_loss_scaling_steps = int(math.log2(opts.loss_scaling // opts.initial_loss_scaling)) + 1
    loss_scaling_steps = {i * (opts.epoch // num_loss_scaling_steps) + 1: opts.initial_loss_scaling * (2 ** i) for i in range(num_loss_scaling_steps)}
    new_loss_scaling = old_loss_scaling = opts.initial_loss_scaling

    if opts.mixup_enabled or opts.cutmix_enabled:
        augmentation_generator = np.random.default_rng(opts.seed)


    for epoch in epochs:

        logging.info(f"Epoch {epoch}/{opts.epoch}")

        if opts.disable_metrics or (opts.use_popdist and not(opts.popdist_rank == 0)):
            bar = training_data
        else:
            bar = tqdm(training_data, total=iterations_per_epoch)

        epoch_start_time = time.time()
        total_sample = 0
        if epoch in loss_scaling_steps.keys():
            new_loss_scaling = loss_scaling_steps[epoch]
        for batch_idx, (input_data, labels) in enumerate(bar):
            if opts.mixup_enabled or opts.cutmix_enabled:
                input_data = get_augmented_samples(opts, input_data, augmentation_generator)

            # Offline model call to compile and exit with sample data
            if opts.compile_only:
                _ = training_model.compile(input_data, labels)
                logging.info("Graph compilation complete, --compile-only was set, exiting.")
                sys.exit(0)

            accuracy, losses, sublosses = training_model(input_data, labels)
            if opts.profile:
                # generate profile report for one iteration
                sys.exit(0)

            epoch_num = epoch - 1 + float(batch_idx+1) / iterations_per_epoch

            if not opts.disable_metrics:
                with torch.no_grad():
                    # Each replica returns sum of values. Need to normalize by gradient accumulation and device iteration.
                    mean_loss = torch.mean(losses).item() / (opts.device_iterations * opts.gradient_accumulation)
                    classification_loss = torch.mean(sublosses[0]).item() / (opts.device_iterations * opts.gradient_accumulation)
                    smoothing_loss = torch.mean(sublosses[1]).item() / (opts.device_iterations * opts.gradient_accumulation) if len(sublosses) > 1 else 0.0
                    acc = torch.mean(accuracy).item() / (opts.device_iterations * opts.gradient_accumulation)
                metrics.save_value("accuracy", acc)
                metrics.save_value("loss", mean_loss)
                metrics.save_value("classification_loss", classification_loss)
                metrics.save_value("smoothing_loss", smoothing_loss)

                aggregated_loss = metrics.get_running_mean("loss")
                aggregated_acc = metrics.get_running_mean("accuracy")
                if not opts.use_popdist or opts.popdist_rank == 0:
                    bar.set_description(f"Loss:{aggregated_loss:0.4f} | Accuracy:{aggregated_acc:0.2f}%")
            total_sample += labels.size()[0] * num_instances

            if not opts.disable_metrics and ((batch_idx + 1) % (iterations_per_epoch // opts.logs_per_epoch) == 0):
                elapsed_time = metrics.get_elapsed_time()
                num_batches = metrics.get_count()
                if validation_function is not None and (epoch % opts.validation_frequency == 0):
                    training_model.detachFromDevice()
                    validation_accuracy = validation_function()
                    model.train()
                    training_model.attachToDevice()
                    logging.info(f"Validation Accuracy: {validation_accuracy:0.2f}%")
                else:
                    validation_accuracy = 0.0
                # save metrics
                result_dict = {
                    "loss_avg": metrics.get_running_mean("loss"),
                    "loss_batch": metrics.get_value("loss"),
                    "epoch": epoch_num,
                    "iteration": batch_idx+1+(epoch-1)*iterations_per_epoch,
                    "train_accuracy_avg": metrics.get_running_mean("accuracy"),
                    "train_accuracy_batch": metrics.get_value("accuracy"),
                    "learning_rate": old_lr,
                    "loss_scaling": old_loss_scaling,
                    "train_img_per_sec": (num_batches * labels.size()[0] / elapsed_time),
                    "latency_sec": elapsed_time / (num_batches * num_instances),
                    "validation_accuracy": validation_accuracy,
                    "classification_loss_batch": metrics.get_value("classification_loss"),
                    "classification_loss_avg": metrics.get_running_mean("classification_loss"),
                    "smoothing_loss_batch:": metrics.get_value("smoothing_loss"),
                    "smoothing_loss_avg:": metrics.get_running_mean("smoothing_loss"),
                }
                if opts.wandb_weight_histogram:
                    utils.Logger.log_model_histogram(training_model.model)
                utils.Logger.log_train_results(result_dict)

            # lr schedule
            lr_scheduler.step(epoch_num)
            new_lr = lr_scheduler.get_last_lr()[0]
            if new_lr != old_lr or new_loss_scaling != old_loss_scaling:
                if new_loss_scaling != old_loss_scaling:
                    optimizer.loss_scaling = new_loss_scaling
                    if opts.optimizer == 'sgd_combined':
                        optimizer.param_groups[0]["velocity_scaling"] = new_loss_scaling / opts.loss_velocity_scaling_ratio
                        optimizer.param_groups[1]["velocity_scaling"] = new_loss_scaling / opts.loss_velocity_scaling_ratio
                training_model.setOptimizer(optimizer)
                old_lr = new_lr
                old_loss_scaling = new_loss_scaling
                if opts.lr_schedule == "step":
                    logging.info(f"Learning rate is changed to {new_lr}")

        epoch_end_time = time.time()
        if not opts.disable_metrics:
            aggregated_acc = metrics.get_running_mean("accuracy")
            logging.info(f"Epoch {epoch}: Train accuracy is {aggregated_acc:0.2f}%")
        elapsed_time = epoch_end_time-epoch_start_time
        # sync metrics
        if opts.use_popdist:
            total_sample = utils.sync_metrics(total_sample)
            elapsed_time = utils.sync_metrics(elapsed_time)
        epoch_throughput = total_sample / elapsed_time
        logging.info(f"Throughput of the epoch:{epoch_throughput:0.1f} img/sec")
        # save, in case of multiple processes save the weights only from the first instance.
        if not opts.checkpoint_path == "" and (not opts.use_popdist or opts.popdist_local_rank == 0):
            if not os.path.exists(opts.checkpoint_path):
                os.makedirs(opts.checkpoint_path)
            filename = f"{opts.model}_{opts.data}_{epoch}.pt"
            save_path = os.path.join(opts.checkpoint_path, filename)
            torch.save({
                'epoch': epoch,
                'model_state_dict': models.get_model_state_dict(training_model),
                'optimizer_state_dict': optimizer.state_dict(),
                'loss': aggregated_loss,
                'train_accuracy': aggregated_acc,
                'opts': opts
            }, save_path)


def get_augmented_samples(opts, input_data, random_generator):
    # Mixup coefficients are sampled on the host, cutmix coefficients are
    # sampled on the device.
    batch_and_mixup_coefficients = [input_data]
    if opts.mixup_enabled:
        mixup_coeffs = augmentations.sample_mixup_coefficients(
            alpha=opts.mixup_alpha,
            batch_size=opts.batch_size * opts.gradient_accumulation * opts.replicas * opts.device_iterations,
            np_type=np.float16 if opts.precision[:3] == "16." else np.float,
            random_generator=random_generator,
        )
        batch_and_mixup_coefficients.append(mixup_coeffs)
    return tuple(batch_and_mixup_coefficients)


def create_training_model_opts(opts):
    if opts.use_popdist:
        model_opts = popdist.poptorch.Options(ipus_per_replica=len(opts.pipeline_splits) + 1)
    else:
        model_opts = poptorch.Options()
        model_opts.replicationFactor(opts.replicas)
    model_opts = utils.train_settings(opts, model_opts)
    model_opts.deviceIterations(opts.device_iterations)
    # Set mean reduction
    model_opts.Training.accumulationAndReplicationReductionType(poptorch.ReductionType.Mean)
    model_opts.Training.gradientAccumulation(opts.gradient_accumulation)

    if opts.seed is not None:
        model_opts.randomSeed(opts.seed)

    # Use offline IPU for compilation only, needs appropriate version
    if opts.compile_only:
        model_opts.useOfflineIpuTarget(poptorch.ipuHardwareVersion())

    return model_opts


def convert_to_ipu_model(model, opts, optimizer):
    model_opts = create_training_model_opts(opts)
    model_with_loss = TrainingModelWithLoss(model, label_smoothing=opts.label_smoothing, use_mixup=opts.mixup_enabled, use_cutmix=opts.cutmix_enabled)
    training_model = poptorch.trainingModel(model_with_loss, model_opts, optimizer=optimizer)
    return training_model


def get_optimizer(opts, model):
    regularized_params = []
    non_regularized_params = []

    # Filter biases and norm parameters.
    for param in model.parameters():
        if param.requires_grad:
            if len(param.shape) == 1:
                non_regularized_params.append(param)
            else:
                regularized_params.append(param)

    params = [
        {'params': regularized_params, 'weight_decay': opts.weight_decay},
        {'params': non_regularized_params, 'weight_decay': 0}
    ]
    if opts.optimizer == 'LAMB':
        params[1]['max_weight_norm'] = 0

    optimizer = None
    if opts.optimizer == 'sgd':
        optimizer = SGD(
            params, lr=opts.lr, momentum=opts.momentum,
            loss_scaling=opts.initial_loss_scaling, use_combined_accum=False)
    elif opts.optimizer == 'sgd_combined':
        optimizer = SGD(
            params, lr=opts.lr, momentum=opts.momentum,
            loss_scaling=opts.initial_loss_scaling,
            velocity_scaling=opts.initial_loss_scaling / opts.loss_velocity_scaling_ratio,
            use_combined_accum=True)
    elif opts.optimizer == 'adamw':
        optimizer = AdamW(
            params, lr=opts.lr,
            loss_scaling=opts.initial_loss_scaling, eps=opts.optimizer_eps)
    elif opts.optimizer == 'rmsprop':
        optimizer = RMSprop(
            params, lr=opts.lr, alpha=opts.rmsprop_decay, momentum=opts.momentum,
            loss_scaling=opts.initial_loss_scaling, eps=opts.optimizer_eps)
    elif opts.optimizer == 'rmsprop_tf':
        optimizer = RMSprop(
            params, lr=opts.lr, alpha=opts.rmsprop_decay, momentum=opts.momentum,
            loss_scaling=opts.initial_loss_scaling, eps=opts.optimizer_eps, use_tf_variant=True)
    elif opts.optimizer == 'lamb':
        optimizer = LAMB(
            params, lr=opts.lr,
            loss_scaling=opts.initial_loss_scaling,
            eps=opts.optimizer_eps,
            max_weight_norm=None)
        optimizer.variable_attrs.markAsConstant("max_weight_norm")
    # Make optimizers distributed
    if opts.use_popdist:
        hvd.broadcast_parameters(model.state_dict(), root_rank=0)
    return optimizer


def get_validation_function(opts, model):
    if opts.validation_mode == "none":
        return None

    if opts.mixup_enabled or opts.cutmix_enabled:
        assert isinstance(model, augmentations.AugmentationModel)
        model = model.model

    inference_model_opts = create_validation_model_opts(opts, use_popdist=opts.use_popdist)
    test_data = datasets.get_data(opts, inference_model_opts, train=False, async_dataloader=True, return_remaining=True)
    inference_model = poptorch.inferenceModel(model, inference_model_opts)

    def validation_func():
        model.eval()
        if inference_model._executable:
            inference_model.attachToDevice()
        val_acc = test(inference_model, test_data, opts)
        if opts.use_popdist:
            val_acc = utils.sync_metrics(val_acc)
        inference_model.detachFromDevice()
        return val_acc
    return validation_func


def get_lr_scheduler(opts, optimizer, step_per_epoch, start_epoch=0):
    scheduler_freq = opts.lr_scheduler_freq if opts.lr_scheduler_freq > 0.0 else step_per_epoch
    scheduler_last_epoch = (scheduler_freq * start_epoch) - 1
    if opts.lr_schedule == "step":
        lr_scheduler = MultiStepLR(
            optimizer=optimizer, milestones=[step*scheduler_freq for step in opts.lr_epoch_decay],
            gamma=opts.lr_decay, last_epoch=scheduler_last_epoch)
    elif opts.lr_schedule == "cosine":
        lr_scheduler = CosineAnnealingLR(
            optimizer=optimizer, T_max=opts.epoch*scheduler_freq, last_epoch=scheduler_last_epoch)
    elif opts.lr_schedule == "exponential":
        lr_scheduler = ExponentialLR(
            optimizer=optimizer, gamma=opts.lr_decay, last_epoch=scheduler_last_epoch)

    lr_scheduler = PeriodicLRDecorator(optimizer=optimizer, lr_scheduler=lr_scheduler, period=1./scheduler_freq)
    lr_scheduler = WarmUpLRDecorator(optimizer=optimizer, lr_scheduler=lr_scheduler, warmup_epoch=opts.warmup_epoch)
    return lr_scheduler


if __name__ == '__main__':
    run_opts = parse_arguments()

    logging.info("Loading the data")
    model_opts = create_training_model_opts(run_opts)
    train_data = datasets.get_data(run_opts, model_opts, train=True, async_dataloader=True)

    logging.info("Initialize the model")
    model = models.get_model(run_opts, datasets.datasets_info[run_opts.data], pretrained=False, use_mixup=run_opts.mixup_enabled, use_cutmix=run_opts.cutmix_enabled)

    model.train()

    optimizer = get_optimizer(run_opts, model)
    lr_scheduler = get_lr_scheduler(run_opts, optimizer, len(train_data))
    training_model = convert_to_ipu_model(model, run_opts, optimizer)

    training_validation_func = get_validation_function(run_opts, model) if run_opts.validation_mode == "during" else None
    train(training_model, train_data, run_opts, lr_scheduler, range(1, run_opts.epoch+1), optimizer, training_validation_func)

    # Weight averaging runs on single process
    if run_opts.weight_avg_strategy != 'none' and (not run_opts.use_popdist or run_opts.popdist_rank == 0):
        average_fn = weight_avg.create_average_fn(run_opts)
        weight_avg.average_model_weights(run_opts.checkpoint_path, average_fn, run_opts.weight_avg_N)

    if run_opts.validation_mode == "after":
        if run_opts.checkpoint_path == "":
            training_model.destroy()
            val_func = get_validation_function(run_opts, model)
            acc = val_func()
            result_dict = {"validation_epoch": run_opts.epoch,
                           "validation_iteration": run_opts.logs_per_epoch * run_opts.epoch,
                           "validation_accuracy": acc}
            utils.Logger.log_validate_results(result_dict)
        else:
            training_model.destroy()
            if not run_opts.use_popdist or run_opts.popdist_rank == 0:
                checkpoint_files = [os.path.join(run_opts.checkpoint_path, file_name) for file_name in os.listdir(run_opts.checkpoint_path) if file_name.endswith(".pt")]
                validate_checkpoints(checkpoint_files)
