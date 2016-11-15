# Copyright 2012 OpenStack Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
"""Wrapper for keystone.common.config that configures itself on import."""

import os

from oslo_config import cfg

from wormhole import paths
from wormhole import versions


_DEFAULT_SQL_CONNECTION = 'sqlite:///' + paths.state_path_def('wormhole.sqlite')

CONF = cfg.CONF

opts = [
    cfg.IntOpt('port',
               default=7127, help='Port of wormhole rest service.'),
    ]

CONF.register_opts(opts)


def parse_args(argv, default_config_files=None):
    CONF(argv[1:],
         project='wormhole',
         version=versions.version_string(),
         default_config_files=default_config_files)
