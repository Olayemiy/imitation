"""Behavioural Cloning (BC).

Trains policy by applying supervised learning to a fixed dataset of (observation,
action) pairs generated by some expert demonstrator.
"""

import os
import sys
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Tuple, Type, Union

import gym
import torch as th
import torch.utils.data as th_data
import tqdm.autonotebook as tqdm
from stable_baselines3.common import logger, policies, preprocessing, utils
from torch.optim import lr_scheduler

from imitation.data import types
from imitation.policies import base
from imitation.util import util


def reconstruct_policy(
    policy_path: str,
    device: Union[th.device, str] = "auto",
) -> policies.BasePolicy:
    """Reconstruct a saved policy.

    Args:
        policy_path: path where `.save_policy()` has been run.
        device: device on which to load the policy.

    Returns:
        policy: policy with reloaded weights.
    """
    policy = th.load(policy_path, map_location=utils.get_device(device))
    assert isinstance(policy, policies.BasePolicy)
    return policy


class ConstantLRSchedule:
    """A callable that returns a constant learning rate."""

    def __init__(self, lr: float = 1e-3):
        """
        Args:
            lr: the constant learning rate that calls to this object will return.
        """
        self.lr = lr

    def __call__(self, _):
        """
        Returns the constant learning rate.
        """
        return self.lr


class EpochOrBatchIteratorWithProgress:
    def __init__(
        self,
        data_loader: Iterable[dict],
        n_epochs: Optional[int] = None,
        n_batches: Optional[int] = None,
        use_tqdm: Optional[bool] = None,
        on_epoch_end: Optional[Callable[[], None]] = None,
        on_batch_end: Optional[Callable[[], None]] = None,
    ):
        """Wraps DataLoader so that all BC batches can be processed in a one for-loop.

        Optionally uses `tqdm` to show progress in stdout.

        Args:
            data_loader: An iterable over data dicts, as used in `BC`.
            n_epochs: The number of epochs to iterate through in one call to
                __iter__. Exactly one of `n_epochs` and `n_batches` should be provided.
            n_batches: The number of batches to iterate through in one call to
                __iter__. Exactly one of `n_epochs` and `n_batches` should be provided.
            use_tqdm: Show a tqdm progress bar if True. True by default if stdout is a
                TTY.
            on_epoch_end: A callback function without parameters to be called at the
                end of every epoch.
            on_batch_end: A callback function without parameters to be called at the
                end of every batch.
        """
        if n_epochs is not None and n_batches is None:
            self.use_epochs = True
        elif n_epochs is None and n_batches is not None:
            self.use_epochs = False
        else:
            raise ValueError(
                "Must provide exactly one of `n_epochs` and `n_batches` arguments."
            )

        self.data_loader = data_loader
        self.n_epochs = n_epochs
        self.n_batches = n_batches
        self.use_tqdm = os.isatty(sys.stdout.fileno()) if use_tqdm is None else use_tqdm
        self.on_epoch_end = on_epoch_end
        self.on_batch_end = on_batch_end

    def __iter__(self) -> Iterable[Tuple[dict, dict]]:
        """Yields batches while updating tqdm display to display progress."""

        samples_so_far = 0
        epoch_num = 0
        batch_num = 0
        display = None
        batch_suffix = epoch_suffix = ""
        if self.use_tqdm:
            if self.use_epochs:
                display = tqdm.tqdm(total=self.n_epochs)
                epoch_suffix = f"/{self.n_epochs}"
            else:  # Use batches.
                display = tqdm.tqdm(total=self.n_batches)
                batch_suffix = f"/{self.n_batches}"

        def update_desc():
            assert display is not None
            display.set_description(
                f"batch: {batch_num}{batch_suffix}  epoch: {epoch_num}{epoch_suffix}"
            )

        try:
            while True:
                if display is not None:
                    update_desc()
                got_data_on_epoch = False
                for batch in self.data_loader:
                    got_data_on_epoch = True
                    batch_num += 1
                    batch_size = len(batch["obs"])
                    assert batch_size > 0
                    samples_so_far += batch_size
                    stats = dict(
                        epoch_num=epoch_num,
                        batch_num=batch_num,
                        samples_so_far=samples_so_far,
                    )
                    yield batch, stats
                    if self.on_batch_end is not None:
                        self.on_batch_end()
                    if not self.use_epochs:
                        if display is not None:
                            update_desc()
                            display.update(1)
                        if batch_num >= self.n_batches:
                            return
                if not got_data_on_epoch:
                    raise AssertionError(
                        f"Data loader returned no data after "
                        f"{batch_num} batches, during epoch "
                        f"{epoch_num} -- did it reset correctly?"
                    )
                epoch_num += 1
                if self.on_epoch_end is not None:
                    self.on_epoch_end()

                if self.use_epochs:
                    if display is not None:
                        update_desc()
                        display.update(1)
                    if epoch_num >= self.n_epochs:
                        return

        finally:
            if display is not None:
                display.close()


class BC:

    DEFAULT_BATCH_SIZE: int = 32
    """Default batch size for DataLoader automatically constructed from Transitions.

    See `set_expert_data_loader()`.
    """

    # TODO(scottemmons): pass BasePolicy into BC directly (rather than passing its
    #  arguments)
    def __init__(
        self,
        observation_space: gym.Space,
        action_space: gym.Space,
        *,
        policy_class: Type[policies.ActorCriticPolicy] = base.FeedForward32Policy,
        policy_kwargs: Optional[Mapping[str, Any]] = None,
        expert_data: Union[Iterable[Mapping], types.TransitionsMinimal, None] = None,
        optimizer_cls: Type[
            th.optim.Optimizer
        ] = th.optim.Adam,  # pytype: disable=module-attr
        optimizer_kwargs: Optional[Dict[str, Any]] = None,
        lr_scheduler_cls: Optional[Type[lr_scheduler._LRScheduler]] = None,
        lr_scheduler_kwargs: Optional[Mapping[str, Any]] = None,
        ent_weight: float = 1e-3,
        l2_weight: float = 0.0,
        augmentation_fn: Callable[[th.Tensor], th.Tensor] = None,
        normalize_images: bool = True,
        device: Union[str, th.device] = "auto",  # pytype: disable=module-attr
    ):
        """Behavioral cloning (BC).

        Recovers a policy via supervised learning on observation-action Tensor
        pairs, sampled from a Torch DataLoader or any Iterator that ducktypes
        `torch.utils.data.DataLoader`.

        Args:
            observation_space: the observation space of the environment.
            action_space: the action space of the environment.
            policy_class: used to instantiate imitation policy.
            policy_kwargs: keyword arguments passed to policy's constructor.
            expert_data: If not None, then immediately call
                  `self.set_expert_data_loader(expert_data)` during initialization.
            optimizer_cls: Optimizer to use for supervised training.
            optimizer_kwargs: Keyword arguments, excluding learning rate and
                  weight decay, for optimizer construction.
            lr_scheduler_cls: A subclass of `_LRScheduler`.
            lr_scheduler_kwargs: Keyword arguments for scheduler construction.
            ent_weight: scaling applied to the policy's entropy regularization.
            l2_weight: scaling applied to the policy's L2 regularization.
            augmentation_fn: function to augment a batch of (on-device) images
                (default: identity).
            device: name/identity of device to place policy on.
        """
        if optimizer_kwargs:
            if "weight_decay" in optimizer_kwargs:
                raise ValueError("Use the parameter l2_weight instead of weight_decay.")

        self.action_space = action_space
        self.observation_space = observation_space
        self.policy_class = policy_class
        self.device = device = utils.get_device(device)

        # SB3's `ActorCriticPolicy` automatically initializes a optimizer which
        # requires an argument of this type, but which we don't use. LR is set
        # to huge value so that it loudly causes errors if we _do_ use it.
        unused_lr_schedule = ConstantLRSchedule(sys.float_info.max)

        self.policy_kwargs = dict(
            observation_space=self.observation_space,
            action_space=self.action_space,
            lr_schedule=unused_lr_schedule,
        )
        self.policy_kwargs.update(policy_kwargs or {})
        self.device = utils.get_device(device)

        self.policy = self.policy_class(**self.policy_kwargs).to(
            self.device,
        )  # pytype: disable=not-instantiable
        optimizer_kwargs = optimizer_kwargs or {}
        self.optimizer = optimizer_cls(self.policy.parameters(), **optimizer_kwargs)

        if lr_scheduler_cls is None:
            self.lr_scheduler = None
            self.epoch_end_step_callback = None
        else:
            self.lr_scheduler = lr_scheduler_cls(
                optimizer=self.optimizer,
                **lr_scheduler_kwargs,
            )
            self.epoch_end_step_callback = lambda **kwargs: self.lr_scheduler.step()

        self.expert_data_loader: Optional[Iterable[Mapping]] = None
        self.ent_weight = ent_weight
        self.l2_weight = l2_weight
        if augmentation_fn is None:
            augmentation_fn = util.identity
        self.augmentation_fn = augmentation_fn

        if expert_data is not None:
            self.set_expert_data_loader(expert_data)

    def set_expert_data_loader(
        self,
        expert_data: Union[Iterable[Mapping], types.TransitionsMinimal],
    ) -> None:
        """Set the expert data loader, which yields batches of obs-act pairs.

        Changing the expert data loader on-demand is useful for DAgger and other
        interactive algorithms.

        Args:
             expert_data: Either a Torch `DataLoader`, any other iterator that
                yields dictionaries containing "obs" and "acts" Tensors or Numpy arrays,
                or a `TransitionsMinimal` instance.

                If this is a `TransitionsMinimal` instance, then it is automatically
                converted into a shuffled `DataLoader` with batch size
                `BC.DEFAULT_BATCH_SIZE`.
        """
        if isinstance(expert_data, types.TransitionsMinimal):
            self.expert_data_loader = th_data.DataLoader(
                expert_data,
                shuffle=True,
                batch_size=BC.DEFAULT_BATCH_SIZE,
                collate_fn=types.transitions_collate_fn,
            )
        else:
            self.expert_data_loader = expert_data

    def _calculate_loss(
        self,
        obs: th.Tensor,
        acts: th.Tensor,
    ) -> Tuple[th.Tensor, Dict[str, float]]:
        """
        Calculate the supervised learning loss used to train the behavioral clone.

        Args:
            obs: The observations seen by the expert. Gradients are detached
                first before loss is calculated.
            acts: The actions taken by the expert. Gradients are detached first
                before loss is calculated.

        Returns:
            loss: The supervised learning loss for the behavioral clone to optimize.
            stats_dict: Statistics about the learning process to be logged.
        """
        obs = obs.detach()
        acts = acts.detach()

        _, log_prob, entropy = self.policy.evaluate_actions(obs, acts)
        prob_true_act = th.exp(log_prob).mean()
        log_prob = log_prob.mean()
        ent_loss = entropy = entropy.mean()

        l2_norms = [th.sum(th.square(w)) for w in self.policy.parameters()]
        l2_loss_raw = sum(l2_norms) / 2  # divide by 2 to cancel grad of square

        ent_term = -self.ent_weight * ent_loss
        neglogp = -log_prob
        l2_term = self.l2_weight * l2_loss_raw
        loss = neglogp + ent_term + l2_term

        stats_dict = dict(
            neglogp=neglogp.item(),
            loss=loss.item(),
            prob_true_act=prob_true_act.item(),
            ent_loss_raw=entropy.item(),
            ent_loss_term=ent_term.item(),
            l2_loss_raw=l2_loss_raw.item(),
            l2_loss_term=l2_term.item(),
        )

        return loss, stats_dict

    def _calculate_policy_norms(
        self, norm_type: Union[int, float] = 2
    ) -> Tuple[th.Tensor, th.Tensor]:
        """
        Calculate the gradient norm and the weight norm of the policy network.

        Args:
            norm_type: order of the norm.

        Returns:
            gradient_norm: norm of the gradient of the policy network (stored in each
                parameter's .grad attribute)
            weight_norm: norm of the weights of the policy network
        """

        norm_type = float(norm_type)

        gradient_parameters = list(
            filter(lambda p: p.grad is not None, self.policy.parameters())
        )
        stacked_gradient_norms = th.stack(
            [
                th.norm(p.grad.detach(), norm_type).to(self.policy.device)
                for p in gradient_parameters
            ]
        )
        stacked_weight_norms = th.stack(
            [
                th.norm(p.detach(), norm_type).to(self.policy.device)
                for p in self.policy.parameters()
            ]
        )

        gradient_norm = th.norm(stacked_gradient_norms, norm_type)
        weight_norm = th.norm(stacked_weight_norms, norm_type)

        return gradient_norm, weight_norm

    def train(
        self,
        *,
        n_epochs: Optional[int] = None,
        n_batches: Optional[int] = None,
        on_epoch_end: Callable[[], None] = None,
        on_batch_end: Callable[[], None] = None,
        log_interval: int = 100,
    ):
        """Train with supervised learning for some number of epochs.

        Here an 'epoch' is just a complete pass through the expert data loader,
        as set by `self.set_expert_data_loader()`.

        Args:
            n_epochs: Number of complete passes made through expert data before ending
                training. Provide exactly one of `n_epochs` and `n_batches`.
            n_batches: Number of batches loaded from dataset before ending training.
                Provide exactly one of `n_epochs` and `n_batches`.
            on_epoch_end: Optional callback with no parameters to run at the end of each
                epoch.
            on_batch_end: Optional callback with no parameters to run at the end of each
                batch.
            log_interval: Log stats after every log_interval batches.
        """
        on_epoch_end = util.join_callbacks(on_epoch_end, self.epoch_end_step_callback)
        it = EpochOrBatchIteratorWithProgress(
            self.expert_data_loader,
            n_epochs=n_epochs,
            n_batches=n_batches,
            on_epoch_end=on_epoch_end,
            on_batch_end=on_batch_end,
        )

        batch_num = 0
        self.policy.train()
        for batch, stats_dict_it in it:
            # some later code (e.g. augmentation, and RNNs if we use them)
            # require contiguous tensors, hence the .contiguous()
            acts_tensor = (
                th.as_tensor(batch["acts"]).contiguous().to(self.policy.device)
            )
            obs_tensor = th.as_tensor(batch["obs"]).contiguous().to(self.policy.device)
            obs_tensor = preprocessing.preprocess_obs(
                obs_tensor,
                self.observation_space,
                normalize_images=True,
            )
            # we always apply augmentations to observations
            obs_tensor = self.augmentation_fn(obs_tensor)
            # FIXME(sam): SB policies *always* apply preprocessing, so we
            # need to undo the preprocessing we did before applying
            # augmentations. The code below is the inverse of SB's
            # preprocessing.preprocess_obs, but only for Box spaces.
            if isinstance(self.observation_space, gym.spaces.Box):
                if preprocessing.is_image_space(self.observation_space):
                    obs_tensor = obs_tensor * 255.0

            loss, stats_dict = self._calculate_loss(obs_tensor, acts_tensor)

            self.optimizer.zero_grad()
            loss.backward()
            self.optimizer.step()

            gradient_norm, weight_norm = self._calculate_policy_norms()
            stats_dict["grad_norm"] = gradient_norm.item()
            stats_dict["weight_norm"] = weight_norm.item()
            stats_dict["n_updates"] = batch_num
            stats_dict["batch_size"] = len(obs_tensor)
            stats_dict["lr_gmean"] = util.optim_lr_gmean(self.optimizer)

            for k, v in stats_dict.items():
                logger.record_mean(k, v)
            if batch_num % log_interval == 0:
                logger.dump(batch_num)

            batch_num += 1

    def save_policy(self, policy_path: str) -> None:
        """Save policy to a path. Can be reloaded by `.reconstruct_policy()`.

        Args:
            policy_path: path to save policy to.
        """
        th.save(self.policy, policy_path)
