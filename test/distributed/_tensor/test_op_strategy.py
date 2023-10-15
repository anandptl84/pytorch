# Owner(s): ["oncall: distributed"]

import torch
from torch.distributed._tensor import DeviceMesh
from torch.distributed._tensor._collective_utils import redistribute_cost
from torch.distributed._tensor.ops.basic_strategy import (
    EinsumDims,
    gen_einsum_strategies,
)
from torch.distributed._tensor.placement_types import (
    _Partial,
    DTensorSpec,
    Replicate,
    Shard,
    TensorMeta,
)

from torch.testing._internal.common_utils import run_tests, TestCase
from torch.testing._internal.distributed._tensor.common_dtensor import DTensorOpTestBase


class TestEinsumDims(TestCase):
    def test_batch_dims(self):
        equation = "abc,abc->abc"
        input_dims, output_dim = EinsumDims.parse_equation(equation)
        edims = EinsumDims.parse_dims(input_dims, output_dim)

        self.assertEqual(edims.batch_dims, ["a", "b", "c"])
        self.assertEqual(edims.contracting_dims, [])
        self.assertEqual(edims.lhs_out_only_dims, [])
        self.assertEqual(edims.rhs_out_only_dims, [])

    def test_mm_dims(self):
        equation = "mk,kn->mn"
        input_dims, output_dim = EinsumDims.parse_equation(equation)
        edims = EinsumDims.parse_dims(input_dims, output_dim)

        self.assertEqual(edims.batch_dims, [])
        self.assertEqual(edims.contracting_dims, ["k"])
        self.assertEqual(edims.lhs_out_only_dims, ["m"])
        self.assertEqual(edims.rhs_out_only_dims, ["n"])

    def test_bmm_dims(self):
        equation = "bmk,bkn->bmn"
        input_dims, output_dim = EinsumDims.parse_equation(equation)
        edims = EinsumDims.parse_dims(input_dims, output_dim)

        self.assertEqual(edims.batch_dims, ["b"])
        self.assertEqual(edims.contracting_dims, ["k"])
        self.assertEqual(edims.lhs_out_only_dims, ["m"])
        self.assertEqual(edims.rhs_out_only_dims, ["n"])

        equation = "bcmk,bckn->bcmn"
        input_dims, output_dim = EinsumDims.parse_equation(equation)
        edims = EinsumDims.parse_dims(input_dims, output_dim)

        self.assertEqual(edims.batch_dims, ["b", "c"])
        self.assertEqual(edims.contracting_dims, ["k"])
        self.assertEqual(edims.lhs_out_only_dims, ["m"])
        self.assertEqual(edims.rhs_out_only_dims, ["n"])

    def test_free_dims(self):
        equation = "abc,ab->abc"
        input_dims, output_dim = EinsumDims.parse_equation(equation)
        edims = EinsumDims.parse_dims(input_dims, output_dim)

        self.assertEqual(edims.batch_dims, ["a", "b"])
        self.assertEqual(edims.contracting_dims, [])
        self.assertEqual(edims.lhs_out_only_dims, ["c"])
        self.assertEqual(edims.rhs_out_only_dims, [])

        equation = "abd,bf->abfd"
        input_dims, output_dim = EinsumDims.parse_equation(equation)
        edims = EinsumDims.parse_dims(input_dims, output_dim)

        self.assertEqual(edims.batch_dims, ["b"])
        self.assertEqual(edims.contracting_dims, [])
        self.assertEqual(edims.lhs_out_only_dims, ["a", "d"])
        self.assertEqual(edims.rhs_out_only_dims, ["f"])


class TestEinsumStrategies(DTensorOpTestBase):
    @property
    def world_size(self) -> int:
        return 4

    def test_mm_1d_mesh(self):
        mesh = self.build_device_mesh()

        all_strats = gen_einsum_strategies("mk,kn->mn", mesh)
        self.assertEqual(len(all_strats.strategies), 4)

    def test_mm_2d_mesh(self):
        mesh = DeviceMesh(self.device_type, torch.arange(self.world_size).reshape(2, 2))

        all_strats = gen_einsum_strategies("mk,kn->mn", mesh)
        self.assertEqual(len(all_strats.strategies), 16)

    def test_bmm_1d_mesh(self):
        mesh = self.build_device_mesh()

        all_strats = gen_einsum_strategies("bmk,bkn->bmn", mesh)
        self.assertEqual(len(all_strats.strategies), 5)

    def test_bmm_2d_mesh(self):
        mesh = DeviceMesh(self.device_type, torch.arange(self.world_size).reshape(2, 2))

        all_strats = gen_einsum_strategies("bmk,bkn->bmn", mesh)
        self.assertEqual(len(all_strats.strategies), 25)

    def test_pointwise_1d_mesh(self):
        mesh = self.build_device_mesh()

        simple_strats = gen_einsum_strategies("abcd,abcd->abcd", mesh)
        self.assertEqual(len(simple_strats.strategies), 5)

        broadcast_strats = gen_einsum_strategies("bcd,abcd->abcd", mesh)
        self.assertEqual(len(broadcast_strats.strategies), 5)

    def test_linearity_1d_mesh(self):
        mesh = self.build_device_mesh()

        all_strats = gen_einsum_strategies("abcd,abcd->abcd", mesh, linearity=True)
        self.assertEqual(len(all_strats.strategies), 6)


class TestCostModel(DTensorOpTestBase):
    def _extract_tensor_meta(self, t) -> TensorMeta:
        return TensorMeta(t.shape, t.stride(), t.dtype)

    @property
    def world_size(self) -> int:
        return 4

    def test_redistribute_cost_mesh_1d(self):
        mesh_1d = self.build_device_mesh()
        shard_placement = (Shard(0),)
        replica_placement = (Replicate(),)
        partial_placement = (_Partial(),)

        global_tensor = torch.randn(10, 10)
        global_tensor_meta = self._extract_tensor_meta(global_tensor)

        # shard spec
        shard_spec = DTensorSpec(mesh_1d, shard_placement, global_tensor_meta)
        # replica spec
        replica_spec = DTensorSpec(mesh_1d, replica_placement, global_tensor_meta)
        # partial spec
        partial_spec = DTensorSpec(mesh_1d, partial_placement, global_tensor_meta)

        # make sure reshard cost is 0 for the same spec redistribute
        for spec in [shard_spec, replica_spec, partial_spec]:
            cost = redistribute_cost(spec, spec)
            self.assertEqual(cost, 0)

        # shard -> replicate
        allgather_cost = redistribute_cost(shard_spec, replica_spec)
        # partial -> shard
        reduce_scatter_cost = redistribute_cost(partial_spec, shard_spec)
        # partial -> replicate
        allreduce_cost = redistribute_cost(partial_spec, replica_spec)
        self.assertEqual(allgather_cost, reduce_scatter_cost)
        self.assertEqual(allreduce_cost + 1, allgather_cost + reduce_scatter_cost)
        # shard to partial
        cost = redistribute_cost(shard_spec, partial_spec)
        self.assertEqual(cost, float("inf"))

    def test_redistribute_cost_mesh_2d(self):
        mesh_2d = DeviceMesh(
            self.device_type, torch.arange(self.world_size).reshape(2, 2)
        )
        shard_placement = (Shard(0), Shard(0))
        replica_placement = (Replicate(), Replicate())
        partial_placement = (_Partial(), _Partial())

        global_tensor = torch.randn(8, 8)
        global_tensor_meta = self._extract_tensor_meta(global_tensor)

        # shard spec
        shard_spec = DTensorSpec(mesh_2d, shard_placement, global_tensor_meta)
        # replica spec
        replica_spec = DTensorSpec(mesh_2d, replica_placement, global_tensor_meta)
        # partial spec
        partial_spec = DTensorSpec(mesh_2d, partial_placement, global_tensor_meta)

        # make sure reshard cost is 0 for the same spec redistribute
        for spec in [shard_spec, replica_spec, partial_spec]:
            cost = redistribute_cost(spec, spec)
            self.assertEqual(cost, 0)

        # shard -> replicate
        allgather_cost = redistribute_cost(shard_spec, replica_spec)
        # partial -> replicate
        allreduce_cost = redistribute_cost(partial_spec, replica_spec)
        # partial -> shard
        reduce_scatter_cost = redistribute_cost(partial_spec, shard_spec)
        self.assertTrue(allreduce_cost > allgather_cost)
        self.assertTrue(allreduce_cost > reduce_scatter_cost)


if __name__ == "__main__":
    run_tests()