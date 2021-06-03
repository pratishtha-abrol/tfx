# Lint as: python2, python3
# Copyright 2019 Google LLC. All Rights Reserved.
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
"""Tests for tfx.examples.penguin.penguin_pipeline_kubeflow_gcp."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
from unittest import mock

import tensorflow as tf
from tfx.dsl.io import fileio
from tfx.examples.penguin import penguin_pipeline_kubeflow
from tfx.orchestration.kubeflow import test_utils as kubeflow_test_utils
from tfx.orchestration.kubeflow.kubeflow_dag_runner import KubeflowDagRunner
from tfx.utils import test_case_utils


class PenguinPipelineKubeflowTest(test_case_utils.TfxTest):

  def setUp(self):
    super().setUp()
    self.enter_context(test_case_utils.change_working_dir(self.tmp_dir))

  @mock.patch('tfx.components.util.udf_utils.UserModuleFilePipDependency.'
              'resolve')
  def testPenguinPipelineConstructionAndDefinitionFileExists(
      self, resolve_mock):
    # Avoid actually performing user module packaging because a placeholder
    # GCS bucket is used.
    resolve_mock.side_effect = lambda pipeline_root: None

    local_logical_pipeline = penguin_pipeline_kubeflow.create_pipeline(
        pipeline_name=penguin_pipeline_kubeflow._pipeline_name,
        pipeline_root=penguin_pipeline_kubeflow._pipeline_root,
        data_root=penguin_pipeline_kubeflow._data_root,
        module_file=penguin_pipeline_kubeflow._module_file,
        enable_tuning=True,
        ai_platform_training_args=penguin_pipeline_kubeflow
        ._ai_platform_training_args,
        ai_platform_serving_args=penguin_pipeline_kubeflow
        ._ai_platform_serving_args,
        beam_pipeline_args=penguin_pipeline_kubeflow._beam_pipeline_args,
        run_env='env_local')
    self.assertEqual(10, len(local_logical_pipeline.components))

    KubeflowDagRunner().run(local_logical_pipeline)
    file_path = os.path.join(self.tmp_dir, 'penguin_kubeflow_gcp.tar.gz')
    self.assertTrue(fileio.exists(file_path))

    gcp_logical_pipeline = penguin_pipeline_kubeflow.create_pipeline(
        pipeline_name=penguin_pipeline_kubeflow._pipeline_name,
        pipeline_root=penguin_pipeline_kubeflow._pipeline_root,
        data_root=penguin_pipeline_kubeflow._data_root,
        module_file=penguin_pipeline_kubeflow._module_file,
        enable_tuning=True,
        ai_platform_training_args=penguin_pipeline_kubeflow
        ._ai_platform_training_args,
        ai_platform_serving_args=penguin_pipeline_kubeflow
        ._ai_platform_serving_args,
        beam_pipeline_args=penguin_pipeline_kubeflow._beam_pipeline_args,
        run_env='env_gcp')
    self.assertEqual(10, len(gcp_logical_pipeline.components))

    KubeflowDagRunner().run(gcp_logical_pipeline)
    file_path = os.path.join(self.tmp_dir, 'penguin_kubeflow_gcp.tar.gz')
    self.assertTrue(fileio.exists(file_path))


class PenguinPipelineEndToEndTest(kubeflow_test_utils.BaseKubeflowTest):

  def testEndToEndPipelineRun(self):
    """End-to-end test for pipeline with RuntimeParameter."""
    gcp_logical_pipeline = penguin_pipeline_kubeflow.create_pipeline(
        pipeline_name=penguin_pipeline_kubeflow._pipeline_name,
        pipeline_root=penguin_pipeline_kubeflow._pipeline_root,
        data_root=penguin_pipeline_kubeflow._data_root,
        module_file=penguin_pipeline_kubeflow._module_file,
        enable_tuning=True,
        ai_platform_training_args=penguin_pipeline_kubeflow
        ._ai_platform_training_args,
        ai_platform_serving_args=penguin_pipeline_kubeflow
        ._ai_platform_serving_args,
        beam_pipeline_args=penguin_pipeline_kubeflow._beam_pipeline_args,
        run_env='env_gcp')

    parameters = {
        'pipeline-root':
            self._pipeline_root(penguin_pipeline_kubeflow._pipeline_name),
        'transform-module':
            self._transform_module,
        'trainer-module':
            self._trainer_module,
        'data-root':
            self._data_root,
        'train-steps':
            10,
        'eval-steps':
            5,
        'slicing-column':
            'species',
    }
    self._compile_and_run_pipeline(
        pipeline=gcp_logical_pipeline, parameters=parameters)


if __name__ == '__main__':
  tf.test.main()
