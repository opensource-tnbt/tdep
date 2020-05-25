# Copyright (c) 2015 Mirantis Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import copy
import datetime
import os
import re
import tempfile

import yaml

from oslo_config import cfg
from oslo_config import types
from utilities import utils


DEFAULT_POLLING_INTERVAL = 10
SCENARIOS = 'templates/l2_1c_1i.yaml'
SCHEMAS = 'templates/'

class Endpoint(types.String):

    def __call__(self, value):
        value = str(value)
        if not re.match(r'\S+:\d+', value):
            raise ValueError('Wrong value of server_endpoint, '
                             'expected <host>:<port>, but got: %s' % value)
        return value

    def __repr__(self):
        return "Endpoint <host:port>"


class Yaml(types.String):

    def __call__(self, value):
        value = str(value)
        try:
            value = yaml.safe_load(value)
        except Exception:
            raise ValueError('YAML value is expected, but got: %s' % value)
        return value

    def __repr__(self):
        return "YAML data"


def generate_output_name():
    file_name = "TESTVNF_%s.json" % utils.strict(str(datetime.datetime.now()))
    return os.path.join(tempfile.gettempdir(), file_name)


COMMON_OPTS = [
    cfg.StrOpt('trafficgen',
            default='trex',
            required=False,
            help='Traffic-generator used in vsperf'),
    cfg.IntOpt('polling-interval',
               default=(utils.env('TESTVNF_POLLING_INTERVAL') or
                        DEFAULT_POLLING_INTERVAL),
               help='How frequently the agent polls server, in seconds')
]

OPENSTACK_OPTS = [
    cfg.StrOpt('os-auth-url', metavar='<auth-url>',
               default=utils.env('OS_AUTH_URL'),
               sample_default='',
               help='Authentication URL, defaults to env[OS_AUTH_URL].'),
    cfg.StrOpt('os-tenant-name', metavar='<auth-tenant-name>',
               default=utils.env('OS_TENANT_NAME'),
               sample_default='',
               help='Authentication tenant name, defaults to '
                    'env[OS_TENANT_NAME].'),
    cfg.StrOpt('os-project-name', metavar='<auth-project-name>',
               default=utils.env('OS_PROJECT_NAME'),
               sample_default='',
               help='Authentication project name. This option is '
                    'mutually exclusive with --os-tenant-name. '
                    'Defaults to env[OS_PROJECT_NAME].'),
    cfg.StrOpt('os-project-domain-name', metavar='<auth-project-domain-name>',
               default=utils.env('OS_PROJECT_DOMAIN_NAME'),
               sample_default='Default',
               help='Authentication project domain name. '
                    'Defaults to env[OS_PROJECT_DOMAIN_NAME].'),
    cfg.StrOpt('os-username', metavar='<auth-username>',
               default=utils.env('OS_USERNAME'),
               sample_default='',
               help='Authentication username, defaults to env[OS_USERNAME].'),
    cfg.StrOpt('os-user-domain-name', metavar='<auth-user-domain-name>',
               default=utils.env('OS_USER_DOMAIN_NAME'),
               sample_default='',
               help='Authentication username. Defaults to '
                    'env[OS_USER_DOMAIN_NAME].'),
    cfg.StrOpt('os-identity-api-version', metavar='<identity-api-version>',
               default=utils.env('OS_IDENTITY_API_VERSION'),
               sample_default='3',
               help='Identity API version, defaults to '
                    'env[OS_IDENTITY_API_VERSION].'),
    cfg.StrOpt('os-password', metavar='<auth-password>',
               default=utils.env('OS_PASSWORD'),
               sample_default='',
               help='Authentication password, defaults to env[OS_PASSWORD].'),
    cfg.StrOpt('os-cacert', metavar='<auth-cacert>',
               default=utils.env('OS_CACERT'),
               sample_default='',
               help='Location of CA Certificate, defaults to env[OS_CACERT].'),
    cfg.BoolOpt('os-insecure',
                default=(utils.env('OS_INSECURE') or False),
                help='When using SSL in connections to the registry server, '
                     'do not require validation via a certifying authority, '
                     'defaults to env[OS_INSECURE].'),
    cfg.StrOpt('os-region-name', metavar='<auth-region-name>',
               default=utils.env('OS_REGION_NAME') or 'RegionOne',
               help='Authentication region name, defaults to '
                    'env[OS_REGION_NAME].'),
    cfg.StrOpt('os-interface', metavar='<os-interface>',
               default=utils.env('OS_INTERFACE'),
               sample_default='',
               help='Interface type. Valid options are public, '
                    'admin and internal. defaults to '
                    'env[OS_INTERFACE].'),
    cfg.StrOpt('os-profile', metavar='<hmac-key>',
               default=utils.env('OS_PROFILE'),
               sample_default='',
               help='HMAC key for encrypting profiling context data, '
                    'defaults to env[OS_PROFILE].'),

    cfg.StrOpt('external-net',
               default=utils.env('TESTVNF_EXTERNAL_NET'),
               help='Name or ID of external network, defaults to '
                    'env[TESTVNF_EXTERNAL_NET]. If no value provided then '
                    'TESTVNF picks any of available external networks.'),

    cfg.ListOpt('dns-nameservers',
                default=['8.8.8.8', '8.8.4.4'],
                help='Comma-separated list of IPs of the DNS nameservers '
                     'for the subnets. If no value is provided defaults to '
                     ' Google Public DNS.'),

    cfg.StrOpt('image-name',
               default=utils.env('TESTVNF_IMAGE') or 'testvnf_img',
               help='Name of image to use. The default is created by '
                    'TESTVNF-image-builder.'),
    cfg.StrOpt('flavor-name',
               default=utils.env('TESTVNF_FLAVOR') or 'TESTVNF-flavor',
               help='Name of image flavor. The default is created by '
                    'TESTVNF-image-builder.'),
    cfg.StrOpt('stack-name',
               default=utils.env('TESTVNF_STACK_NAME') or None,
               help='Name of test heat stack. The default is a uniquely '
                    'generated name.'),
    cfg.StrOpt('reuse-stack-name',
               default=utils.env('TESTVNF_REUSE_STACK_NAME') or None,
               help='Name of an existing TESTVNF heat stack to reuse. The '
                    'default is to not reuse an existing stack. Caution '
                    'should be taken to only reuse stacks meant for a '
                    'specific scenario. Also certain configs e.g. '
                    'image-name, flavor-name, stack-name, etc will be '
                    'ignored when reusing an existing stack.'),
    cfg.BoolOpt('cleanup-on-exit',
                default=(utils.env('TESTVNF_CLEANUP_ON_EXIT') or True),
                help='Clean up the heat-stack when exiting execution.'),
]

CLEANUP_OPTS = [
    cfg.BoolOpt('cleanup',
                default=(utils.env('TESTVNF_CLEANUP') or True),
                help='Cleanup the image and the flavor.'),
]

SERVER_AGENT_OPTS = [
    cfg.IntOpt('agent-loss-timeout',
               default=utils.env('TESTVNF_AGENT_LOSS_TIMEOUT') or 60,
               help='Timeout to treat agent as lost in seconds, '
                    'defaults to env[TESTVNF_AGENT_LOSS_TIMEOUT]'),
    cfg.IntOpt('agent-join-timeout',
               default=utils.env('TESTVNF_AGENT_JOIN_TIMEOUT') or 600,
               help='Timeout to treat agent as join failed in seconds, '
                    'defaults to env[TESTVNF_AGENT_JOIN_TIMEOUT] (time '
                    'between stack deployment and start of scenario '
                    'execution).'),
]

SCENARIO_OPTS = [
    cfg.ListOpt('scenario',
                #default=utils.env('TESTVNF_SCENARIO'),
                default=SCENARIOS,
                required=True,
                help=utils.make_help_options(
                    'Comma-separated list of scenarios to play. Each entity '
                    'can be a file name or one of aliases: '
                    '%s. Defaults to env[TESTVNF_SCENARIO].', SCENARIOS,
                    type_filter=lambda x: (x.endswith('.yaml') and
                                           not x.startswith('test/')))),
    cfg.Opt('matrix',
            default=utils.env('TESTVNF_MATRIX'),
            type=Yaml(),
            help='Set the matrix of parameters for the scenario. The value '
                 'is specified in YAML format. E.g. to override the scenario '
                 'duration one may provide: "{time: 10}", or to override list '
                 'of hosts: "{host:[ping.online.net, iperf.eenet.ee]}". When '
                 'several parameters are overridden all combinations are '
                 'tested'),
    cfg.StrOpt('output',
               default=utils.env('TESTVNF_OUTPUT') or generate_output_name(),
               sample_default='',
               help='File for output in JSON format, '
                    'defaults to env[TESTVNF_OUTPUT]. If it is empty, then '
                    'output will be saved to '
                    '/tmp/TESTVNF_<time_now>.json'),
    cfg.StrOpt('artifacts-dir', default=utils.env('TESTVNF_ARTIFACTS_DIR'),
               help='If specified, directs TESTVNF to store there all its '
                    'artifacts (output, report, subunit and book). '
                    'Defaults to env[TESTVNF_ARTIFACTS_DIR].'),
    cfg.BoolOpt('no-report-on-error',
                deprecated_for_removal=True,
                default=(utils.env('TESTVNF_NO_REPORT_ON_ERROR') or False),
                help='Do not generate report for failed scenarios'),
    cfg.ListOpt('scenario-availability-zone',
                default=utils.env('SCENARIO_AVAILABILITY_ZONE'),
                help='Comma-separated list of availability_zone. If specified '
                     'this setting will override the availability_zone '
                     'accomodation setting in the scenario test definition.'
                     'Defaults to SCENARIO_AVAILABILITY_ZONE'),
    cfg.IntOpt('scenario-compute-nodes',
               default=utils.env('SCENARIO_COMPUTE_NODES'),
               help='Number of compute_nodes. If specified this setting will '
                    'override the compute_nodes accomodation setting in the '
                    'scenario test definition. '
                    'Defaults to SCENARIO_COMPUTE_NODES'),
    cfg.Opt('custom-user-opts',
            default=utils.env('CUSTOM_USER_OPTS'),
            type=Yaml(),
            help='Set custom user option parameters for the scenario. '
                 'The value is specified in YAML, e.g. '
                 'custom_user_opts = { key1:value1, key2:value2} '
                 'The values specified can be referenced in the usual '
                 'python way. e.g. {{ CONF.custom_user_opts[\'key1\'] }}. '
                 'This option is useful to inject custom values into heat '
                 'environment files'),
]

AGENT_OPTS = [
    cfg.StrOpt('agent-id',
               default=utils.env('TESTVNF_AGENT_ID'),
               help='Agent unique id, defaults to MAC of primary interface.'),
]

SERVER_OPTS = SCENARIO_OPTS + SERVER_AGENT_OPTS


def list_opts():
    all_opts = (OPENSTACK_OPTS + IMAGE_BUILDER_OPTS +
                SERVER_OPTS + AGENT_OPTS + CLEANUP_OPTS)
    yield (None, copy.deepcopy(all_opts))
