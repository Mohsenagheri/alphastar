# Copyright 2021 DeepMind Technologies Limited.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Common modules used in training and evaluation."""

import threading
from typing import Any, Callable, List, Mapping, Optional, Sequence

from absl import logging
from acme import core
from acme.tf import savers as tf_savers
from acme.utils import loggers as acme_loggers
from alphastar.collections import jax as jax_collections
import chex
import ml_collections
import pandas as pd
import tensorflow as tf


jax_collections.register_struct()


class FramesLimitedCheckpointingRunner(tf_savers.CheckpointingRunner):
  """Extension of CheckpointingRunner terminating on number of frames."""

  def __init__(self, num_frames_per_step: int, max_number_of_frames: int,
               **kwargs):
    super().__init__(**kwargs)
    self._max_number_of_frames = max_number_of_frames
    self._num_frames_per_step = num_frames_per_step

  def run(self):
    state = self.save()
    while state.step * self._num_frames_per_step < self._max_number_of_frames:
      self.step()
      state = self.save()


class LockedIterator(object):
  """Wrapper around an iterator to guarantee thread-safety."""

  def __init__(self, iterator):
    self._lock = threading.Lock()
    self._iterator = iter(iterator)

  def __iter__(self):
    return self

  def __next__(self):
    with self._lock:
      return next(self._iterator)


class MockSaveableLearner(core.Saveable):

  def __init__(self, state: Optional[chex.Array] = None):
    self._state = state

  def save(self):
    return self._state

  def restore(self, state):
    self._state = state


def get_standard_loggers(
    label: str,
    log_to_csv: bool = True,
    print_fn: Optional[Callable[[str], None]] = None,
) -> List[acme_loggers.Logger]:
  """Makes default Acme logger.

  This is a logger that will write to logs, the terminal, and to bigtable if
  running a deepmind job under xmanager.
  This implementation is similar to `acme.utils.loggers.make_default_logger()`
  Args:
    label: Name to give to the logger.
    log_to_csv : Where to log expt data to CSV.local:
    print_fn: How to print to terminal (defaults to absl.logging).

  Returns:
    A list of standard acme logger objects.
  """
  if not print_fn:
    print_fn = print

  terminal_logger = acme_loggers.TerminalLogger(label, print_fn=print_fn)
  loggers = [terminal_logger]

  if log_to_csv:
    logging.info('logging CSV files under (%s)', label)
    loggers.append(acme_loggers.CSVLogger(label=label))
  return loggers


def aggregate_and_filter_logs(
    loggers: List[acme_loggers.Logger],
    asynchronous: bool,
    time_delta: float,
    serialize_fn: Optional[Callable[[Mapping[str, Any]],
                                    str]] = acme_loggers.to_numpy
) -> acme_loggers.Logger:
  """Aggregates and filters logs.

  Args:
    loggers: A list of acme logger objects
    asynchronous: Whether the write function should block or not.
    time_delta: Time (in seconds) between logging events.
    serialize_fn: An optional function to apply to the write inputs before
      passing them to the various loggers.

  Returns:
    An ACME logger object.
  """
  # Dispatch to all writers and filter Nones and by time.
  logger = acme_loggers.aggregators.Dispatcher(loggers, serialize_fn)
  logger = acme_loggers.NoneFilter(logger)
  if asynchronous:
    logger = acme_loggers.AsyncLogger(logger)
  logger = acme_loggers.TimeFilter(logger, time_delta)
  return logger


def make_default_logger(
    label: str,
    log_to_csv: bool = True,
    time_delta: float = 1.0,
    asynchronous: bool = False,
    print_fn: Optional[Callable[[str], None]] = None,
    serialize_fn: Optional[Callable[[Mapping[str, Any]],
                                    str]] = acme_loggers.to_numpy,
) -> acme_loggers.Logger:
  """Make a default Acme logger.

  This is a logger that will write to logs, the terminal, and to bigtable if
  running a deepmind job under xmanager.
  This implementation is similar to `acme.utils.loggers.make_default_logger()`
  Args:
    label: Name to give to the logger.
    log_to_csv : Where to log expt data to CSV.
    time_delta: Time (in seconds) between logging events.
    asynchronous: Whether the write function should block or not.
    print_fn: How to print to terminal (defaults to absl.logging).
    serialize_fn: An optional function to apply to the write inputs before
      passing them to the various loggers.

  Returns:
    A logger object that responds to logger.write(some_dict).
  """
  loggers = get_standard_loggers(
      label=label, log_to_csv=log_to_csv, print_fn=print_fn)

  logger = aggregate_and_filter_logs(
      loggers=loggers, asynchronous=asynchronous, time_delta=time_delta,
      serialize_fn=serialize_fn)
  return logger


def restore_from_checkpoint(wrapped: core.Saveable,
                            checkpoint_to_restore: Optional[str] = None,
                            fields_to_restore: Optional[Sequence[str]] = None):
  """Restore specified fields for the state from a checkpoint."""
  # This will output the learner's state.
  if isinstance(fields_to_restore, Sequence) and not fields_to_restore:
    return wrapped

  if checkpoint_to_restore:
    wrapped_object_state = wrapped.save()
    checkpointable_wrapped = tf_savers.SaveableAdapter(wrapped)
    objects_to_save = {'wrapped': checkpointable_wrapped}
    ckpt = tf.train.Checkpoint(**objects_to_save)
    logging.info('Restoring from checkpoint %s', checkpoint_to_restore)
    ckpt.restore(checkpoint_to_restore)

    if fields_to_restore is not None and wrapped_object_state is not None:
      wrapped_object_state_from_ckpt = wrapped.save()
      # Replace only those fields of the state from checkpoint which you need.
      modified_fields = {
          field: getattr(wrapped_object_state_from_ckpt, field)
          for field in fields_to_restore
      }
      wrapped_object_state = wrapped_object_state._replace(**modified_fields)
      wrapped.restore(wrapped_object_state)
  return wrapped


def flatten_metrics(metrics_nest):
  return pd.json_normalize(metrics_nest, sep='_').to_dict(orient='records')[0]


def validate_config(config: ml_collections.ConfigDict,
                    launch_args: Sequence[str]):
  """Validates a config."""
  args_as_dict = dict(
      [arg.split('=', maxsplit=1) for arg in launch_args if '=' in arg])
  arch_str = args_as_dict['--config'].split(':')[-1]
  if arch_str != config.architecture.name:
    raise ValueError(f'Architecture string in config tag [{arch_str}] and '
                     f'config.architecture.name [{config.architecture.name}] '
                     'need to be consistent.')
