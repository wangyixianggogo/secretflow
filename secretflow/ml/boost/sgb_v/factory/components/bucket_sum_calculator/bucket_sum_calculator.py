# Copyright 2023 Ant Group Co., Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from dataclasses import dataclass
from typing import Dict, List, Tuple

from secretflow.data import FedNdarray
from secretflow.device import PYU, HEUObject, PYUObject

from ....core.pure_numpy_ops.bucket_sum import batch_select_sum, regroup_bucket_sums
from ....core.pure_numpy_ops.grad import split_GH
from ..cache.level_wise_cache import LevelWiseCache
from ..component import Composite, Devices
from ..gradient_encryptor import GradientEncryptor
from ..shuffler import Shuffler


@dataclass
class BucketSumCalculatorComponents:
    level_wise_cache: LevelWiseCache = LevelWiseCache()


class BucketSumCalculator(Composite):
    def __init__(self):
        self.components = BucketSumCalculatorComponents()

    def show_params(self):
        return

    def set_params(self, _: dict):
        return

    def get_params(self, _: dict):
        return

    def set_devices(self, devices: Devices):
        super().set_devices(devices)
        self.label_holder = devices.label_holder
        self.workers = devices.workers
        self.party_num = len(self.workers)

    def calculate_bucket_sum_level_wise(
        self,
        shuffler: Shuffler,
        encrypted_gh_dict: Dict[PYU, HEUObject],
        children_split_node_selects: List[PYUObject],
        is_lefts: List[bool],
        order_map_sub: FedNdarray,
        bucket_num: int,
        bucket_lists: List[PYUObject],
        gradient_encryptor: GradientEncryptor,
        node_num: int,
    ) -> Tuple[PYUObject, PYUObject]:
        bucket_sums_list = [[] for _ in range(self.party_num)]
        bucket_num_plus_one = bucket_num + 1
        shuffler.reset_shuffle_masks()
        self.components.level_wise_cache.reset_level_caches()
        for i, worker in enumerate(self.workers):
            if worker != self.label_holder:
                bucket_sums = encrypted_gh_dict[worker].batch_feature_wise_bucket_sum(
                    children_split_node_selects,
                    order_map_sub.partitions[worker],
                    bucket_num_plus_one,
                    True,
                )
                self.components.level_wise_cache.collect_level_node_GH(
                    worker, bucket_sums, is_lefts
                )
                bucket_sums = self.components.level_wise_cache.get_level_nodes_GH(
                    worker
                )
                bucket_sums = [
                    bucket_sum[shuffler.create_shuffle_mask(i, j, bucket_lists[i])]
                    for j, bucket_sum in enumerate(bucket_sums)
                ]

                bucket_sums_list[i] = [
                    bucket_sum.to(
                        self.label_holder,
                        gradient_encryptor.get_move_config(self.label_holder),
                    )
                    for bucket_sum in bucket_sums
                ]
            else:
                bucket_sums = self.label_holder(batch_select_sum)(
                    encrypted_gh_dict[worker],
                    children_split_node_selects,
                    order_map_sub.partitions[worker],
                    bucket_num_plus_one,
                )
                self.components.level_wise_cache.collect_level_node_GH(
                    worker, bucket_sums, is_lefts
                )
                bucket_sums = self.components.level_wise_cache.get_level_nodes_GH(
                    worker
                )
                bucket_sums_list[i] = bucket_sums

        level_nodes_G, level_nodes_H = self.label_holder(
            lambda bucket_sums_list, node_num: [
                *zip(
                    *[
                        split_GH(regroup_bucket_sums(bucket_sums_list, idx))
                        for idx in range(node_num)
                    ]
                )
            ],
            num_returns=2,
        )(bucket_sums_list, node_num)
        return level_nodes_G, level_nodes_H

    def update_level_cache(self, is_last_level, gain_is_cost_effective):
        self.components.level_wise_cache.update_level_cache(
            is_last_level, gain_is_cost_effective
        )