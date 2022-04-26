"""Constructs deep network reward models."""

import abc
from typing import Callable, Iterable, Sequence, Tuple, Type

import gym
import numpy as np
import torch as th
from stable_baselines3.common import preprocessing
from torch import nn

from imitation.util import networks


class RewardNet(nn.Module, abc.ABC):
    """Minimal abstract reward network.

    Only requires the implementation of a forward pass (calculating rewards given
    a batch of states, actions, next states and dones).
    """

    def __init__(
        self,
        observation_space: gym.Space,
        action_space: gym.Space,
        normalize_images: bool = True,
    ):
        """Initialize the RewardNet.

        Args:
            observation_space: the observation space of the environment
            action_space: the action space of the environment
            normalize_images: whether to automatically normalize
                image observations to [0, 1] (from 0 to 255). Defaults to True.
        """
        super().__init__()
        self.observation_space = observation_space
        self.action_space = action_space
        self.normalize_images = normalize_images

    @abc.abstractmethod
    def forward(
        self,
        state: th.Tensor,
        action: th.Tensor,
        next_state: th.Tensor,
        done: th.Tensor,
    ) -> th.Tensor:
        """Compute rewards for a batch of transitions and keep gradients."""

    def preprocess(
        self,
        state: np.ndarray,
        action: np.ndarray,
        next_state: np.ndarray,
        done: np.ndarray,
    ) -> Tuple[th.Tensor, th.Tensor, th.Tensor, th.Tensor]:
        """Preprocess a batch of input transitions and convert it to PyTorch tensors.

        The output of this function is suitable for its forward pass,
        so a typical usage would be ``model(*model.preprocess(transitions))``.

        Args:
            state: The observation input. Its shape is
                `(batch_size,) + observation_space.shape`.
            action: The action input. Its shape is
                `(batch_size,) + action_space.shape`. The None dimension is
                expected to be the same as None dimension from `obs_input`.
            next_state: The observation input. Its shape is
                `(batch_size,) + observation_space.shape`.
            done: Whether the episode has terminated. Its shape is `(batch_size,)`.

        Returns:
            Preprocessed transitions: a Tuple of tensors containing
            observations, actions, next observations and dones.
        """
        state_th = th.as_tensor(state, device=self.device)
        action_th = th.as_tensor(action, device=self.device)
        next_state_th = th.as_tensor(next_state, device=self.device)
        done_th = th.as_tensor(done, device=self.device)

        del state, action, next_state, done  # unused

        # preprocess
        state_th = preprocessing.preprocess_obs(
            state_th,
            self.observation_space,
            self.normalize_images,
        )
        action_th = preprocessing.preprocess_obs(
            action_th,
            self.action_space,
            self.normalize_images,
        )
        next_state_th = preprocessing.preprocess_obs(
            next_state_th,
            self.observation_space,
            self.normalize_images,
        )
        done_th = done_th.to(th.float32)

        n_gen = len(state_th)
        assert state_th.shape == next_state_th.shape
        assert len(action_th) == n_gen

        return state_th, action_th, next_state_th, done_th

    def predict(
        self,
        state: np.ndarray,
        action: np.ndarray,
        next_state: np.ndarray,
        done: np.ndarray,
    ) -> np.ndarray:
        """Compute rewards for a batch of transitions without gradients.

        Preprocesses the inputs, converting between Torch
        tensors and NumPy arrays as necessary.

        Args:
            state: Current states of shape `(batch_size,) + state_shape`.
            action: Actions of shape `(batch_size,) + action_shape`.
            next_state: Successor states of shape `(batch_size,) + state_shape`.
            done: End-of-episode (terminal state) indicator of shape `(batch_size,)`.

        Returns:
            Computed rewards of shape `(batch_size,`).
        """
        with networks.evaluating(self):
            # switch to eval mode (affecting normalization, dropout, etc)

            state_th, action_th, next_state_th, done_th = self.preprocess(
                state,
                action,
                next_state,
                done,
            )
            with th.no_grad():
                rew_th = self(state_th, action_th, next_state_th, done_th)

            rew = rew_th.detach().cpu().numpy().flatten()
            assert rew.shape == state.shape[:1]
            return rew

    def predict_processed(
        self,
        state: np.ndarray,
        action: np.ndarray,
        next_state: np.ndarray,
        done: np.ndarray,
    ) -> np.ndarray:
        """Compute the processed rewards for a batch of transitions without gradients.

        Its default behavior in RewardNet is to return the raw rewards from predict().

        Args:
            state: Current states of shape `(batch_size,) + state_shape`.
            action: Actions of shape `(batch_size,) + action_shape`.
            next_state: Successor states of shape `(batch_size,) + state_shape`.
            done: End-of-episode (terminal state) indicator of shape `(batch_size,)`.

        Returns:
            Computed normalized rewards of shape `(batch_size,`).
        """
        return self.predict(state, action, next_state, done)

    @property
    def device(self) -> th.device:
        """Heuristic to determine which device this module is on."""
        try:
            first_param = next(self.parameters())
            return first_param.device
        except StopIteration:
            # if the model has no parameters, we use the CPU
            return th.device("cpu")

    @property
    def dtype(self) -> th.dtype:
        """Heuristic to determine dtype of module."""
        try:
            first_param = next(self.parameters())
            return first_param.dtype
        except StopIteration:
            # if the model has no parameters, default to float32
            return th.get_default_dtype()


class RewardNetWrapper(RewardNet, abc.ABC):
    """A RewardNet wrapper with a base net."""

    def __init__(
        self,
        observation_space: gym.Space,
        action_space: gym.Space,
        base: RewardNet,
        normalize_images: bool = True,
    ):
        """A minimal abstract reward network wrapper with a base net.

        A concrete implementation of forward() is needed.

        Args:
            observation_space: the observation space of the environment
            action_space: the action space of the environment
            base: a base RewardNet
            normalize_images: passed through to `RewardNet.__init__`,
                see its documentation
        """
        super().__init__(
            observation_space,
            action_space,
            normalize_images,
        )
        self._base = base

    @property
    def base(self) -> RewardNet:
        return self._base


class BasicRewardNet(RewardNet):
    """MLP that takes as input the state, action, next state and done flag.

    These inputs are flattened and then concatenated to one another. Each input
    can enabled or disabled by the `use_*` constructor keyword arguments.
    """

    def __init__(
        self,
        observation_space: gym.Space,
        action_space: gym.Space,
        use_state: bool = True,
        use_action: bool = True,
        use_next_state: bool = False,
        use_done: bool = False,
        **kwargs,
    ):
        """Builds reward MLP.

        Args:
            observation_space: The observation space.
            action_space: The action space.
            use_state: should the current state be included as an input to the MLP?
            use_action: should the current action be included as an input to the MLP?
            use_next_state: should the next state be included as an input to the MLP?
            use_done: should the "done" flag be included as an input to the MLP?
            kwargs: passed straight through to `build_mlp`.
        """
        super().__init__(
            observation_space,
            action_space,
        )
        combined_size = 0

        self.use_state = use_state
        if self.use_state:
            combined_size += preprocessing.get_flattened_obs_dim(observation_space)

        self.use_action = use_action
        if self.use_action:
            combined_size += preprocessing.get_flattened_obs_dim(action_space)

        self.use_next_state = use_next_state
        if self.use_next_state:
            combined_size += preprocessing.get_flattened_obs_dim(observation_space)

        self.use_done = use_done
        if self.use_done:
            combined_size += 1

        full_build_mlp_kwargs = {
            "hid_sizes": (32, 32),
        }
        full_build_mlp_kwargs.update(kwargs)
        full_build_mlp_kwargs.update(
            {
                # we do not want these overridden
                "in_size": combined_size,
                "out_size": 1,
                "squeeze_output": True,
            },
        )

        self.mlp = networks.build_mlp(**full_build_mlp_kwargs)

    def forward(self, state, action, next_state, done):
        inputs = []
        if self.use_state:
            inputs.append(th.flatten(state, 1))
        if self.use_action:
            inputs.append(th.flatten(action, 1))
        if self.use_next_state:
            inputs.append(th.flatten(next_state, 1))
        if self.use_done:
            inputs.append(th.reshape(done, [-1, 1]))

        inputs_concat = th.cat(inputs, dim=1)

        outputs = self.mlp(inputs_concat)
        assert outputs.shape == state.shape[:1]

        return outputs


class NormalizedRewardNet(RewardNetWrapper):
    """A reward net that normalizes the output of its base net.

    Only requires the implementation of a forward pass (calculating rewards given
    a batch of states, actions, next states and dones) as it inherits RewardNet.
    """

    def __init__(
        self,
        observation_space: gym.Space,
        action_space: gym.Space,
        base: RewardNet,
        rew_normalize_class: Type[nn.Module],
        normalize_images: bool = True,
    ):
        """Initialize the NormalizedRewardNet.

        Args:
            observation_space: the observation space of the environment
            action_space: the action space of the environment
            base: a base RewardNet
            rew_normalize_class: The class to use to normalize rewards. This can be
                any nn.Module that preserves the shape;
                e.g. `nn.BatchNorm*`, `nn.LayerNorm`, or `networks.RunningNorm`.
            normalize_images: passed through to `RewardNet.__init__`,
                see its documentation
        """
        super().__init__(
            observation_space=observation_space,
            action_space=action_space,
            base=base,
            normalize_images=normalize_images,
        )
        # assuming reward is always a scalar
        self.rew_normalize_layer = (
            rew_normalize_class(1) if rew_normalize_class else None
        )

    def predict_processed(
        self,
        state: np.ndarray,
        action: np.ndarray,
        next_state: np.ndarray,
        done: np.ndarray,
    ) -> np.ndarray:
        """Compute normalized rewards for a batch of transitions without gradients.

        Args:
            state: Current states of shape `(batch_size,) + state_shape`.
            action: Actions of shape `(batch_size,) + action_shape`.
            next_state: Successor states of shape `(batch_size,) + state_shape`.
            done: End-of-episode (terminal state) indicator of shape `(batch_size,)`.

        Returns:
            Computed normalized rewards of shape `(batch_size,`).
        """
        rew = self.base.predict(state, action, next_state, done)
        rew_th = th.as_tensor(rew, device=self.device)
        rew = self.rew_normalize_layer(rew_th).detach().cpu().numpy().flatten()
        assert rew.shape == state.shape[:1]
        return rew

    def forward(
        self,
        state: th.Tensor,
        action: th.Tensor,
        next_state: th.Tensor,
        done: th.Tensor,
    ):
        return self.base(state, action, next_state, done)


class BasicNormalizedRewardNet(NormalizedRewardNet):
    """An implementation of NormalizedRewardNet that uses MLP as its base net."""

    def __init__(
        self,
        observation_space: gym.Space,
        action_space: gym.Space,
        rew_normalize_class: Type[nn.Module],
        *,
        reward_hid_sizes: Sequence[int] = (32,),
        use_state: bool = True,
        use_action: bool = True,
        use_next_state: bool = False,
        use_done: bool = False,
        **kwargs,
    ):
        """Builds a simple shaped reward network.

        Args:
            observation_space: The observation space.
            action_space: The action space.
            rew_normalize_class: The class to use to normalize rewards. This can be
                any nn.Module that preserves the shape;
                e.g. `nn.BatchNorm*`, `nn.LayerNorm`, or `networks.RunningNorm`.
            reward_hid_sizes: sequence of widths for the hidden layers
                of the base reward MLP.
            use_state: should the current state be included as an input
                to the reward MLP?
            use_action: should the current action be included as an input
                to the reward MLP?
            use_next_state: should the next state be included as an input
                to the reward MLP?
            use_done: should the "done" flag be included as an input to the reward MLP?
            kwargs: passed straight through to `BasicRewardNet`.
        """
        build_mlp_kwargs = {
            k: v for k, v in kwargs.items() if k != "rew_normalize_class"
        }

        base_reward_net = BasicRewardNet(
            observation_space=observation_space,
            action_space=action_space,
            use_state=use_state,
            use_action=use_action,
            use_next_state=use_next_state,
            use_done=use_done,
            hid_sizes=reward_hid_sizes,
            **build_mlp_kwargs,
        )

        super().__init__(
            observation_space,
            action_space,
            base=base_reward_net,
            rew_normalize_class=rew_normalize_class,
        )


class ShapedRewardNet(RewardNetWrapper):
    """A RewardNet consisting of a base net and a potential shaping."""

    def __init__(
        self,
        observation_space: gym.Space,
        action_space: gym.Space,
        base: RewardNet,
        potential: Callable[[th.Tensor], th.Tensor],
        discount_factor: float,
        normalize_images: bool = True,
    ):
        """Setup a ShapedRewardNet instance.

        Args:
            observation_space: observation space of the environment
            action_space: action space of the environment
            base: the base reward net to which the potential shaping
                will be added.
            potential: A callable which takes
                a batch of states (as a PyTorch tensor) and returns a batch of
                potentials for these states. If this is a PyTorch Module, it becomes
                a submodule of the ShapedRewardNet instance.
            discount_factor: discount factor to use for the potential shaping
            normalize_images: passed through to `RewardNet.__init__`,
                see its documentation
        """
        super().__init__(
            observation_space=observation_space,
            action_space=action_space,
            base=base,
            normalize_images=normalize_images,
        )
        self.potential = potential
        self.discount_factor = discount_factor

    def forward(
        self,
        state: th.Tensor,
        action: th.Tensor,
        next_state: th.Tensor,
        done: th.Tensor,
    ):
        base_reward_net_output = self.base(state, action, next_state, done)
        new_shaping_output = self.potential(next_state).flatten()
        old_shaping_output = self.potential(state).flatten()
        # NOTE(ejnnr): We fix the potential of terminal states to zero, which is
        # necessary for valid potential shaping in a variable-length horizon setting.
        #
        # In more detail: variable-length episodes are usually modeled
        # as infinite-length episodes where we transition to a terminal state
        # in which we then remain forever. The transition to this final
        # state contributes gamma * Phi(s_T) - Phi(s_{T - 1}) to the returns,
        # where Phi is the potential and s_T the final state. But on every step
        # afterwards, the potential shaping leads to a reward of (gamma - 1) * Phi(s_T).
        # The discounted series of these rewards, which is added to the return,
        # is gamma / (1 - gamma) times this reward, i.e. just -gamma * Phi(s_T).
        # This cancels the contribution of the final state to the last "real"
        # transition, so instead of computing the infinite series, we can
        # equivalently fix the final potential to zero without loss of generality.
        # Not fixing the final potential to zero and also not adding this infinite
        # series of remaining potential shapings can lead to reward shaping
        # that does not preserve the optimal policy if the episodes have variable
        # length!
        new_shaping = (1 - done.float()) * new_shaping_output
        final_rew = (
            base_reward_net_output
            + self.discount_factor * new_shaping
            - old_shaping_output
        )
        assert final_rew.shape == state.shape[:1]
        return final_rew


class BasicShapedRewardNet(ShapedRewardNet):
    """Shaped reward net based on MLPs.

    This is just a very simple convenience class for instantiating a BasicRewardNet
    and a BasicPotentialShaping and wrapping them inside a ShapedRewardNet.
    Mainly exists for backwards compatibility after
    https://github.com/HumanCompatibleAI/imitation/pull/311
    to keep the scripts working.

    TODO(ejnnr): if we ever modify AIRL so that it takes in a RewardNet instance
        directly (instead of a class and kwargs) and instead instantiate the
        RewardNet inside the scripts, then it probably makes sense to get rid
        of this class.

    """

    def __init__(
        self,
        observation_space: gym.Space,
        action_space: gym.Space,
        *,
        reward_hid_sizes: Sequence[int] = (32,),
        potential_hid_sizes: Sequence[int] = (32, 32),
        use_state: bool = True,
        use_action: bool = True,
        use_next_state: bool = False,
        use_done: bool = False,
        discount_factor: float = 0.99,
        **kwargs,
    ):
        """Builds a simple shaped reward network.

        Args:
            observation_space: The observation space.
            action_space: The action space.
            reward_hid_sizes: sequence of widths for the hidden layers
                of the base reward MLP.
            potential_hid_sizes: sequence of widths for the hidden layers
                of the potential MLP.
            use_state: should the current state be included as an input
                to the reward MLP?
            use_action: should the current action be included as an input
                to the reward MLP?
            use_next_state: should the next state be included as an input
                to the reward MLP?
            use_done: should the "done" flag be included as an input to the reward MLP?
            discount_factor: discount factor for the potential shaping.
            kwargs: passed straight through to `BasicRewardNet` and `BasicPotentialMLP`.
        """
        # FIXME(yawen): why could the reward net and potential net use the same kwargs
        # to construct their MDPs?

        # build_mlp doesn't support rew_normalize_class 
        build_mlp_kwargs = {
            k: v for k, v in kwargs.items() if k != "rew_normalize_class"
        }

        base_reward_net = BasicRewardNet(
            observation_space=observation_space,
            action_space=action_space,
            use_state=use_state,
            use_action=use_action,
            use_next_state=use_next_state,
            use_done=use_done,
            hid_sizes=reward_hid_sizes,
            **build_mlp_kwargs,
        )

        potential_net = BasicPotentialMLP(
            observation_space=observation_space,
            hid_sizes=potential_hid_sizes,
            **build_mlp_kwargs,
        )

        super().__init__(
            observation_space,
            action_space,
            base_reward_net,
            potential_net,
            discount_factor=discount_factor,
        )


class BasicPotentialMLP(nn.Module):
    """Simple implementation of a potential using an MLP."""

    def __init__(
        self,
        observation_space: gym.Space,
        hid_sizes: Iterable[int],
        **kwargs,
    ):
        """Initialize the potential.

        Args:
            observation_space: observation space of the environment.
            hid_sizes: widths of the hidden layers of the MLP.
            kwargs: passed straight through to `build_mlp`.
        """
        super().__init__()
        potential_in_size = preprocessing.get_flattened_obs_dim(observation_space)
        self._potential_net = networks.build_mlp(
            in_size=potential_in_size,
            hid_sizes=hid_sizes,
            squeeze_output=True,
            flatten_input=True,
            **kwargs,
        )

    def forward(self, state: th.Tensor) -> th.Tensor:
        return self._potential_net(state)
