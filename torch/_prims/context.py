import functools
from contextlib import nullcontext
from typing import Any, Callable, Dict, Sequence
from warnings import warn

import torch

import torch._decomp
import torch._prims

import torch._refs
import torch._refs.nn
import torch._refs.nn.functional
import torch._refs.special
import torch.overrides
from torch._prims.nvfuser_executor import NvfuserPrimOperatorSupport

from torch._prims_common import torch_function_passthrough
from torch.fx.experimental.proxy_tensor import get_isolated_graphmodule


@functools.lru_cache(None)
def torch_to_refs_map():
    """
    Mapping of torch API functions to torch._refs functions.
    E.g. torch_to_refs_map()[torch.add] == torch._refs.add
    """
    modules = [
        (torch, torch._refs),
        (torch.nn, torch._refs.nn),
        (torch.nn.functional, torch._refs.nn.functional),
        (torch.special, torch._refs.special),
        (torch.fft, torch._refs.fft),
        (torch.linalg, torch._refs.linalg),
    ]
    r: Dict[Any, Any] = {
        torch.Tensor.__invert__: torch._refs.bitwise_not,
        torch.Tensor.__xor__: torch._refs.bitwise_xor,
        torch.Tensor.__and__: torch._refs.bitwise_and,
        torch.Tensor.__or__: torch._refs.bitwise_or,
        torch.Tensor.__eq__: torch._refs.eq,
        torch.Tensor.__rsub__: torch._refs.rsub,
        torch.Tensor.__rtruediv__: torch._refs.rtruediv,
        torch.Tensor.__floordiv__: torch._refs.floor_divide,
        torch.Tensor.__rfloordiv__: torch._refs.rfloordiv,
        torch.Tensor.__pow__: torch._refs.pow,
        torch.Tensor.__rpow__: torch._refs.rpow,
        torch.Tensor.new_empty: torch._refs.new_empty,
        torch.Tensor.new_full: torch._refs.new_full,
        torch.Tensor.new_zeros: torch._refs.new_zeros,
        torch.Tensor.new_ones: torch._refs.new_ones,
        torch.Tensor.fill_: torch._refs.fill_,
        torch.Tensor.zero_: torch._refs.zero_,
        torch.Tensor.to: torch._refs.to,
        torch.Tensor.sum_to_size: torch._refs.sum_to_size,
        # TODO: Should these methods be mapped some other way?
        torch.Tensor.copy_: torch._prims.copy_to,
        torch.Tensor.resize: torch._prims.resize,
    }
    for mod_torch, mod_refs in modules:
        for s in mod_refs.__all__:  # type: ignore[attr-defined]
            r[mod_torch.__dict__.get(s)] = mod_refs.__dict__.get(s)

    # Support remapping torch.Tensor.foo to _refs.foo
    for s in dir(torch.Tensor):
        if s in torch._refs.__all__:
            r[getattr(torch.Tensor, s)] = torch._refs.__dict__.get(s)
    return r


@functools.lru_cache(None)
def all_prims():
    """
    Set of all prim functions, e.g., torch._prims.add in all_prims()
    """
    return {torch._prims.__dict__.get(s) for s in torch._prims.__all__}


class NvfuserPrimsMode(torch.overrides.TorchFunctionMode):
    """
    Switches the interpretation of torch.ops.prims.* functions to
    use nvFuser's prims in torch.ops.nvprims.*

    >>> # xdoctest: +SKIP("undefined vars")
    >>> with NvfuserPrimsMode():
    ...     torch.ops.prims.add(x, y)  # calls torch.ops.nvprims.add(x, y)

    By default, this context manager will fall back on the torch.ops.prims* if the
    nvprim does not exist.
    """

    def __torch_function__(
        self,
        orig_func: Callable,
        types: Sequence,
        args: Sequence[Any] = (),
        kwargs: Dict = None,
    ):
        if kwargs is None:
            kwargs = {}
        if isinstance(orig_func, torch._ops.OpOverload) or isinstance(
            orig_func, torch._ops.OpOverloadPacket
        ):
            namespace = str(orig_func).split(".")[0]
            name = str(orig_func).split(".")[1]
            if namespace == "prims":
                nvfunc = getattr(torch.ops.nvprims, name, None)
                if nvfunc is not None:
                    return nvfunc(*args, **kwargs)
        return orig_func(*args, **kwargs)


class TorchRefsMode(torch.overrides.TorchFunctionMode):
    """
    Switches the interpretation of torch.* functions and Tensor methods to
    use PrimTorch refs in torch._refs.  (Direct calls to _refs are unaffected.)

    >>> # xdoctest: +SKIP
    >>> with TorchRefsMode():
    ...     torch.add(x, y)  # calls torch._refs.add(x, y)

    By default, this context manager will fall back on the torch.* if the
    ref does not exist; set strict=True to error if this occurs.
    If the ref exists we still would like to fall back on the torch.* sometimes,
    this behavior can be customized by passing a function to should_fallback_fn.
    """

    def __init__(
        self,
        strict=False,
        should_fallback_fn=lambda *_: False,
        prims_mode_cls=nullcontext,
    ):
        self.strict = strict
        self.should_fallback_fn = should_fallback_fn
        self.prims_mode_cls = prims_mode_cls

    def __torch_function__(
        self,
        orig_func: Callable,
        types: Sequence,
        args: Sequence[Any] = (),
        kwargs: Dict = None,
    ):
        if kwargs is None:
            kwargs = {}
        # For primitive operations, run them as is without interception
        # Unless we are in prims_mode, in which case we want to use nvprims
        if orig_func in torch_function_passthrough or orig_func in all_prims():
            with self.prims_mode_cls():
                return orig_func(*args, **kwargs)
        mapping = torch_to_refs_map()
        func = mapping.get(orig_func, None)

        # For torch.ops.aten.*, use registered decompositions from torch._decomp
        # torch._decomp.decomposition_table provides a mapping from
        # torch.ops.aten.* to torch._refs or torch._decomp.decompositions
        # implementations.
        # There're other ways to implement this functionality,
        # see https://github.com/pytorch/pytorch/pull/82657#discussion_r939776417
        if func is None and isinstance(orig_func, torch._ops.OpOverload):
            func = torch._decomp.decomposition_table.get(orig_func, None)

        if func is not None:
            # If the ref exists query whether we should use it or not
            if self.should_fallback_fn(self, func, args, kwargs):
                return orig_func(*args, **kwargs)
            # torch calls inside func should be interpreted as refs calls
            with self:
                return func(*args, **kwargs)
        if self.strict:
            raise RuntimeError(
                f"no _refs support for {torch.overrides.resolve_name(orig_func)}"
            )
        return orig_func(*args, **kwargs)


def _is_node_supported_nvfuser(node):
    return (
        node.op == "call_function"
        and getattr(node.target, "impl_nvfuser", None) is not None
    )


def _is_func_unsupported_nvfuser(torch_function_mode, func, args, kwargs):
    with torch_function_mode:
        try:
            gm = get_isolated_graphmodule(func, args, kwargs)
        except Exception as e:
            warn(
                "get_isolated_graphmodule failed on decomposition: "
                + func.__name__
                + " with error message: "
                + str(e)
            )
            # returns unsupported when tracing fails.
            return True

    supported_ops = NvfuserPrimOperatorSupport()
    call_function_nodes = filter(lambda n: n.op == "call_function", gm.graph.nodes)
    any_unsupported = any(
        not supported_ops.is_node_supported(None, node) for node in call_function_nodes
    )
    return any_unsupported


class TorchRefsNvfuserCapabilityMode(TorchRefsMode):
    def __init__(self):
        super().__init__(
            strict=False,
            should_fallback_fn=_is_func_unsupported_nvfuser,
            prims_mode_cls=NvfuserPrimsMode,
        )

    def _is_var_mean(self, func):
        return "torch.var_mean" == torch.overrides.resolve_name(func) or (
            (
                isinstance(func, torch._ops.OpOverload)
                or isinstance(func, torch._ops.OpOverloadPacket)
            )
            and "aten.var_mean" in str(func)
        )

    def _is_rand_like(self, func):
        result = "torch.rand_like" == torch.overrides.resolve_name(func) or (
            func == torch.ops.aten.rand_like or func == torch.ops.aten.rand_like.default
        )
        return result

    def __torch_function__(
        self,
        orig_func: Callable,
        types: Sequence,
        args: Sequence[Any] = (),
        kwargs: Dict = None,
    ):
        if kwargs is None:
            kwargs = {}
        # First we intercept calls for nvfuser-specific prims bypassing generic torch._refs
        if self._is_var_mean(orig_func):
            return torch.ops.nvprims.var_mean(*args, **kwargs)
        if self._is_rand_like(orig_func):
            if len(kwargs) > 0:
                warn("rand_like has ignored kwars!")
            return torch.ops.nvprims.rand_like(*args)
        # Then we use TorchRefsMode to interpret the rest
        return super().__torch_function__(orig_func, types, args, kwargs)
