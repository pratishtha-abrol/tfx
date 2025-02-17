# Copyright 2019 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Tests for tfx.orchestration.kubeflow.kubeflow_dag_runner."""

import json
import os
import tarfile
from typing import Text, List

from kfp import onprem
import tensorflow as tf
from tfx.components.statistics_gen import component as statistics_gen_component
from tfx.dsl.component.experimental import executor_specs
from tfx.dsl.components.base import base_component
from tfx.dsl.io import fileio
from tfx.extensions.google_cloud_big_query.example_gen import component as big_query_example_gen_component
from tfx.orchestration import data_types
from tfx.orchestration import pipeline as tfx_pipeline
from tfx.orchestration.kubeflow import kubeflow_dag_runner
from tfx.proto import example_gen_pb2
from tfx.types import component_spec
from tfx.utils import telemetry_utils
from tfx.utils import test_case_utils
import yaml

from ml_metadata.proto import metadata_store_pb2


# 2-step pipeline under test.
def _two_step_pipeline() -> tfx_pipeline.Pipeline:
  default_input_config = json.dumps({
      'splits': [{
          'name': 'single_split',
          'pattern': 'SELECT * FROM default-table'
      }]
  })
  input_config = data_types.RuntimeParameter(
      name='input_config', ptype=Text, default=default_input_config)
  example_gen = big_query_example_gen_component.BigQueryExampleGen(
      input_config=input_config, output_config=example_gen_pb2.Output())
  statistics_gen = statistics_gen_component.StatisticsGen(
      examples=example_gen.outputs['examples'])
  return tfx_pipeline.Pipeline(
      pipeline_name='two_step_pipeline',
      pipeline_root='pipeline_root',
      metadata_connection_config=metadata_store_pb2.ConnectionConfig(),
      components=[example_gen, statistics_gen],
  )


class _DummySpec(component_spec.ComponentSpec):
  INPUTS = {}
  OUTPUTS = {}
  PARAMETERS = {}


class _DummyComponent(base_component.BaseComponent):
  SPEC_CLASS = _DummySpec
  EXECUTOR_SPEC = executor_specs.TemplatedExecutorContainerSpec(
      image='dummy:latest', command=['ls'])

  def __init__(self):
    super().__init__(_DummySpec())


def _container_component_pipeline() -> tfx_pipeline.Pipeline:
  return tfx_pipeline.Pipeline(
      pipeline_name='container_component_pipeline',
      pipeline_root='pipeline_root',
      metadata_connection_config=metadata_store_pb2.ConnectionConfig(),
      components=[_DummyComponent()],
  )


class KubeflowDagRunnerTest(test_case_utils.TfxTest):

  def setUp(self):
    super().setUp()
    self._source_data_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), 'testdata')
    self.enter_context(test_case_utils.change_working_dir(self.tmp_dir))

  def _compare_tfx_ir_against_testdata(self, args: List[str], golden_file: str):
    index_of_tfx_ir_flag = args.index('--tfx_ir')
    self.assertAllGreater(len(args), index_of_tfx_ir_flag)
    real_tfx_ir = json.loads(args[index_of_tfx_ir_flag + 1])
    real_tfx_ir_str = json.dumps(real_tfx_ir, sort_keys=True)
    with open(os.path.join(self._source_data_dir,
                           golden_file)) as tfx_ir_json_file:
      formatted_tfx_ir = json.dumps(json.load(tfx_ir_json_file), sort_keys=True)
      self.assertEqual(real_tfx_ir_str, formatted_tfx_ir)

  def testTwoStepPipeline(self):
    """Sanity-checks the construction and dependencies for a 2-step pipeline."""
    kubeflow_dag_runner.KubeflowDagRunner().run(_two_step_pipeline())
    file_path = os.path.join(self.tmp_dir, 'two_step_pipeline.tar.gz')
    self.assertTrue(fileio.exists(file_path))

    with tarfile.TarFile.open(file_path).extractfile(
        'pipeline.yaml') as pipeline_file:
      self.assertIsNotNone(pipeline_file)
      pipeline = yaml.safe_load(pipeline_file)

      containers = [
          c for c in pipeline['spec']['templates'] if 'container' in c
      ]
      self.assertEqual(2, len(containers))

      big_query_container = [
          c for c in containers if c['name'] == 'bigqueryexamplegen'
      ]
      self.assertEqual(1, len(big_query_container))
      self.assertEqual([
          'python',
          '-m',
          'tfx.orchestration.kubeflow.container_entrypoint',
      ], big_query_container[0]['container']['command'])
      self.assertIn('--tfx_ir', big_query_container[0]['container']['args'])
      self.assertIn('--node_id', big_query_container[0]['container']['args'])
      self._compare_tfx_ir_against_testdata(
          big_query_container[0]['container']['args'],
          'two_step_pipeline_post_dehydrate_ir.json')

      statistics_gen_container = [
          c for c in containers if c['name'] == 'statisticsgen'
      ]
      self.assertEqual(1, len(statistics_gen_container))

      # Ensure the pod labels are correctly appended.
      metadata = [
          c['metadata'] for c in pipeline['spec']['templates'] if 'dag' not in c
      ]
      for m in metadata:
        self.assertEqual('tfx', m['labels'][telemetry_utils.LABEL_KFP_SDK_ENV])

      # Ensure dependencies between components are captured.
      dag = [c for c in pipeline['spec']['templates'] if 'dag' in c]
      self.assertEqual(1, len(dag))

      self.assertEqual(
          {
              'tasks': [{
                  'name': 'bigqueryexamplegen',
                  'template': 'bigqueryexamplegen',
                  'arguments': {
                      'parameters': [{
                          'name': 'input_config',
                          'value': '{{inputs.parameters.input_config}}'
                      }, {
                          'name': 'pipeline-root',
                          'value': '{{inputs.parameters.pipeline-root}}'
                      }]
                  }
              }, {
                  'name': 'statisticsgen',
                  'template': 'statisticsgen',
                  'arguments': {
                      'parameters': [{
                          'name': 'pipeline-root',
                          'value': '{{inputs.parameters.pipeline-root}}'
                      }]
                  },
                  'dependencies': ['bigqueryexamplegen'],
              }]
          }, dag[0]['dag'])

  def testDefaultPipelineOperatorFuncs(self):
    kubeflow_dag_runner.KubeflowDagRunner().run(_two_step_pipeline())
    file_path = 'two_step_pipeline.tar.gz'
    self.assertTrue(fileio.exists(file_path))

    with tarfile.TarFile.open(file_path).extractfile(
        'pipeline.yaml') as pipeline_file:
      self.assertIsNotNone(pipeline_file)
      pipeline = yaml.safe_load(pipeline_file)

      containers = [
          c for c in pipeline['spec']['templates'] if 'container' in c
      ]
      self.assertEqual(2, len(containers))

  def testMountGcpServiceAccount(self):
    kubeflow_dag_runner.KubeflowDagRunner(
        config=kubeflow_dag_runner.KubeflowDagRunnerConfig(
            pipeline_operator_funcs=kubeflow_dag_runner
            .get_default_pipeline_operator_funcs(use_gcp_sa=True))).run(
                _two_step_pipeline())
    file_path = 'two_step_pipeline.tar.gz'
    self.assertTrue(fileio.exists(file_path))

    with tarfile.TarFile.open(file_path).extractfile(
        'pipeline.yaml') as pipeline_file:
      self.assertIsNotNone(pipeline_file)
      pipeline = yaml.safe_load(pipeline_file)

      containers = [
          c for c in pipeline['spec']['templates'] if 'container' in c
      ]
      self.assertEqual(2, len(containers))

      # Check that each container has default GCP credentials.

      container_0 = containers[0]
      env = [
          env for env in container_0['container']['env']
          if env['name'] == 'GOOGLE_APPLICATION_CREDENTIALS'
      ]
      self.assertEqual(1, len(env))
      self.assertEqual('/secret/gcp-credentials/user-gcp-sa.json',
                       env[0]['value'])

      container_1 = containers[0]
      env = [
          env for env in container_1['container']['env']
          if env['name'] == 'GOOGLE_APPLICATION_CREDENTIALS'
      ]
      self.assertEqual(1, len(env))
      self.assertEqual('/secret/gcp-credentials/user-gcp-sa.json',
                       env[0]['value'])

  def testVolumeMountingPipelineOperatorFuncs(self):
    mount_volume_op = onprem.mount_pvc('my-persistent-volume-claim',
                                       'my-volume-name',
                                       '/mnt/volume-mount-path')
    config = kubeflow_dag_runner.KubeflowDagRunnerConfig(
        pipeline_operator_funcs=[mount_volume_op])

    kubeflow_dag_runner.KubeflowDagRunner(config=config).run(
        _two_step_pipeline())
    file_path = 'two_step_pipeline.tar.gz'
    self.assertTrue(fileio.exists(file_path))

    with tarfile.TarFile.open(file_path).extractfile(
        'pipeline.yaml') as pipeline_file:
      self.assertIsNotNone(pipeline_file)
      pipeline = yaml.safe_load(pipeline_file)

      container_templates = [
          c for c in pipeline['spec']['templates'] if 'container' in c
      ]
      self.assertEqual(2, len(container_templates))

      volumes = [{
          'name': 'my-volume-name',
          'persistentVolumeClaim': {
              'claimName': 'my-persistent-volume-claim'
          }
      }]

      # Check that the PVC is specified for kfp<=0.1.31.1.
      if 'volumes' in pipeline['spec']:
        self.assertEqual(volumes, pipeline['spec']['volumes'])

      for template in container_templates:
        # Check that each container has the volume mounted.
        self.assertEqual([{
            'name': 'my-volume-name',
            'mountPath': '/mnt/volume-mount-path'
        }], template['container']['volumeMounts'])

        # Check that each template has the PVC specified for kfp>=0.1.31.2.
        if 'volumes' in template:
          self.assertEqual(volumes, template['volumes'])

  def testContainerComponent(self):
    kubeflow_dag_runner.KubeflowDagRunner().run(_container_component_pipeline())
    file_path = os.path.join(self.tmp_dir,
                             'container_component_pipeline.tar.gz')
    self.assertTrue(fileio.exists(file_path))

    with tarfile.TarFile.open(file_path).extractfile(
        'pipeline.yaml') as pipeline_file:
      self.assertIsNotNone(pipeline_file)
      pipeline = yaml.safe_load(pipeline_file)
      containers = [
          c for c in pipeline['spec']['templates'] if 'container' in c
      ]
      self.assertLen(containers, 1)
      component_args = containers[0]['container']['args']
      self.assertIn('--node_id', component_args)

if __name__ == '__main__':
  tf.test.main()
