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

import collections
import functools
import random
import sys
import os
import copy
import json
import jinja2
import shutil
import datetime
import time 

from oslo_config import cfg
from oslo_log import log as logging

from utilities import utils
from osclients import heat
from osclients import neutron
from osclients import nova
from osclients import openstack
from config import config

LOG = logging.getLogger(__name__)


class DeploymentException(Exception):
    pass


def prepare_for_cross_az(compute_nodes, zones):
    if len(zones) != 2:
        LOG.warn('cross_az is specified, but len(zones) is not 2')
        return compute_nodes

    masters = []
    slaves = []
    for node in compute_nodes:
        if node['zone'] == zones[0]:
            masters.append(node)
        else:
            slaves.append(node)

    res = []
    for i in range(min(len(masters), len(slaves))):
        res.append(masters[i])
        res.append(slaves[i])

    return res


def generate_agents(compute_nodes, accommodation, unique):
    print(compute_nodes)
    density = accommodation.get('density') or 1

    zones = accommodation.get('zones')
    if zones:
        compute_nodes = [
            c for c in compute_nodes if c['zone'] in zones or
            ':'.join(filter(None, [c['zone'], c['host']])) in zones]
        if 'cross_az' in accommodation:
            # sort nodes to interleave hosts from different zones
            compute_nodes = prepare_for_cross_az(compute_nodes, zones)

    best_effort = accommodation.get('best_effort', False)
    compute_nodes_requested = accommodation.get('compute_nodes')
    if compute_nodes_requested:
        if compute_nodes_requested > len(compute_nodes):
            print(str(len(compute_nodes)))
            if best_effort:
                LOG.warn('Allowing best_effort accommodation: '
                         'compute nodes requested: %(req)d: '
                         'available: %(avail)d available' %
                         dict(req=compute_nodes_requested,
                              avail=len(compute_nodes)))
            else:
                raise DeploymentException(
                    'Not enough compute nodes %(cn)s for requested '
                    'instance accommodation %(acc)s' %
                    dict(cn=compute_nodes, acc=accommodation))
        else:
            compute_nodes = random.sample(compute_nodes,
                                          compute_nodes_requested)

    cn_count = len(compute_nodes)
    iterations = cn_count * density

    if 'single_room' in accommodation and 'pair' in accommodation:
        # special case to allow pair, single_room on single compute node
        if best_effort and iterations == 1:
            LOG.warn('Allowing best_effort accommodation: '
                     'single_room, pair on one compute node')
        else:
            iterations //= 2
    node_formula = lambda x: compute_nodes[x % cn_count]

    agents = {}

    for i in range(iterations):
        if 'pair' in accommodation:
            master_id = '%s_master_%s' % (unique, i)
            slave_id = '%s_slave_%s' % (unique, i)
            master = dict(id=master_id, mode='master', slave_id=slave_id)
            slave = dict(id=slave_id, mode='slave', master_id=master_id)

            if 'single_room' in accommodation:
                master_formula = lambda x: i * 2
                slave_formula = lambda x: i * 2 + 1
            elif 'double_room' in accommodation:
                master_formula = lambda x: i
                slave_formula = lambda x: i
            else:  # mixed_room
                master_formula = lambda x: i
                slave_formula = lambda x: i + 1

            m = node_formula(master_formula(i))
            master['node'], master['zone'] = m['host'], m['zone']
            s = node_formula(slave_formula(i))
            slave['node'], slave['zone'] = s['host'], s['zone']

            agents[master['id']] = master
            agents[slave['id']] = slave
        else:
            if 'single_room' in accommodation:
                agent_id = '%s_agent_%s' % (unique, i)
                agents[agent_id] = dict(id=agent_id,
                                        node=node_formula(i)['host'],
                                        zone=node_formula(i)['zone'],
                                        mode='alone')

    if not agents:
        raise DeploymentException('Not enough compute nodes %(cn)s for '
                                  'requested instance accommodation %(acc)s' %
                                  dict(cn=compute_nodes, acc=accommodation))

    # inject availability zone
    for agent in agents.values():
        az = agent['zone']
        if agent['node']:
            az += ':' + agent['node']
        agent['availability_zone'] = az

    return agents


def _get_stack_values(stack_outputs, vm_name, params):
    result = {}
    for param in params:
        o = stack_outputs.get(vm_name + '_' + param)
        if o:
            result[param] = o
    return result


def filter_agents(agents, stack_outputs, override=None):
    deployed_agents = {}

    # first pass, ignore non-deployed
    for agent in agents.values():
        stack_values = _get_stack_values(stack_outputs, agent['id'], ['ip'])
        new_stack_values = _get_stack_values(stack_outputs, agent['id'], ['pip'])
        mac_values = _get_stack_values(stack_outputs, agent['id'], ['dmac'])

        if override:
            stack_values.update(override(agent))

        if not stack_values.get('ip'):
            LOG.info('Ignore non-deployed agent: %s', agent)
            continue

        if not new_stack_values.get('pip'):
            LOG.info('Ignore non-deployed agent: %s', agent)
            continue

        if not mac_values.get('dmac'):
            LOG.info('Ignore non-deployed agent: %s', agent)
            continue

        agent.update(stack_values)
        agent.update(new_stack_values)

        # workaround of Nova bug 1422686
        if agent.get('mode') == 'slave' and not agent.get('ip'):
            LOG.info('IP address is missing in agent: %s', agent)
            continue

        deployed_agents[agent['id']] = agent

    # second pass, check pairs
    result = {}
    for agent in deployed_agents.values():
        print(agent.get('mode'))
        print(agent.get('ip'))
        print(agent.get('pip'))
        print(agent.get('dmac'))
        if (agent.get('mode') == 'alone' or
                (agent.get('mode') == 'master' and
                 agent.get('slave_id') in deployed_agents) or
                (agent.get('mode') == 'slave' and
                 agent.get('master_id') in deployed_agents)):
            result[agent['id']] = agent

    return result


def distribute_agents(agents, get_host_fn):
    result = {}

    hosts = set()
    buckets = collections.defaultdict(list)
    for agent in agents.values():
        agent_id = agent['id']
        # we assume that server name equals to agent_id
        host_id = get_host_fn(agent_id)

        if host_id not in hosts:
            hosts.add(host_id)
            agent['node'] = host_id
            buckets[agent['mode']].append(agent)
        else:
            LOG.info('Filter out agent %s, host %s is already occupied',
                     agent_id, host_id)

    if buckets['alone']:
        result = dict((a['id'], a) for a in buckets['alone'])
    else:
        for master, slave in zip(buckets['master'], buckets['slave']):
            master['slave_id'] = slave['id']
            slave['master_id'] = master['id']

            result[master['id']] = master
            result[slave['id']] = slave

    return result


def normalize_accommodation(accommodation):
    result = {}

    for s in accommodation:
        if isinstance(s, dict):
            result.update(s)
        else:
            result[s] = True

    # override scenario's availability zone accommodation
    if cfg.CONF.scenario_availability_zone:
        result['zones'] = cfg.CONF.scenario_availability_zone

    # override scenario's compute_nodes accommodation
    if cfg.CONF.scenario_compute_nodes:
        result['compute_nodes'] = cfg.CONF.scenario_compute_nodes

    return result


class Deployment(object):
    def __init__(self):
        self.openstack_client = None
        self.stack_id = None
        self.privileged_mode = True

        # The current run "owns" the support stacks, it is tracked
        # so it can be deleted later.
        self.support_stacks = []
        self.TrackStack = collections.namedtuple('TrackStack', 'name id')

    def connect_to_openstack(self, openstack_params, flavor_name, image_name,
                             external_net, dns_nameservers):
        LOG.debug('Connecting to OpenStack')

        self.openstack_client = openstack.OpenStackClient(openstack_params)

        self.flavor_name = flavor_name
        self.image_name = image_name

        if cfg.CONF.stack_name is not None:
            self.stack_name = cfg.CONF.stack_name
        else:
            self.stack_name = 'testvnf_%s' % utils.random_string()

        if cfg.CONF.reuse_stack_name is not None:
            self.stack_name = cfg.CONF.reuse_stack_name

        self.dns_nameservers = dns_nameservers
        # intiailizing self.external_net last so that other attributes don't
        # remain uninitialized in case user forgets to create external network
        self.external_net = (external_net or
                             neutron.choose_external_net(
                                 self.openstack_client.neutron))

    def _get_compute_nodes(self, accommodation):
        try:
            return nova.get_available_compute_nodes(self.openstack_client.nova,
                                                    self.flavor_name)
        except nova.ForbiddenException:
            # user has no permissions to list compute nodes
            LOG.info('OpenStack user does not have permission to list compute '
                     'nodes - treat him as non-admin')
            self.privileged_mode = False
            count = accommodation.get('compute_nodes')
            if not count:
                raise DeploymentException(
                    'When run with non-admin user the scenario must specify '
                    'number of compute nodes to use')

            zones = accommodation.get('zones') or ['nova']
            return [dict(host=None, zone=zones[n % len(zones)])
                    for n in range(count)]

    #def _deploy_from_hot(self, specification, server_endpoint, base_dir=None):
    def _deploy_from_hot(self, specification,  base_dir=None):
        accommodation = normalize_accommodation(
            specification.get('accommodation') or
            specification.get('vm_accommodation'))

        agents = generate_agents(self._get_compute_nodes(accommodation),
                                 accommodation, self.stack_name)

        # render template by jinja
        vars_values = {
            'agents': agents,
            'unique': self.stack_name,
        }
        heat_template = utils.read_file(specification['template'],
                                        base_dir=base_dir)
        compiled_template = jinja2.Template(heat_template)
        rendered_template = compiled_template.render(vars_values)
        LOG.info('Rendered template: %s', rendered_template)

        # create stack by Heat
        try:
            merged_parameters = {
#                'server_endpoint': server_endpoint,
                'external_net': self.external_net,
                'image': self.image_name,
                'flavor': self.flavor_name,
                'dns_nameservers': self.dns_nameservers,
            }
        except AttributeError as e:
            LOG.error('Failed to gather required parameters to create '
                      'heat stack: %s', e)
            exit(1)

        merged_parameters.update(specification.get('template_parameters', {}))

        env_file = specification.get('env_file', None)
        if env_file is not None:
            env_file = self._render_env_template(env_file, base_dir)

        support_templates = specification.get('support_templates', None)
        if support_templates is not None:
            self._deploy_support_stacks(support_templates, base_dir)

        if cfg.CONF.reuse_stack_name is None:
            try:
                self.stack_id = heat.create_stack(
                    self.openstack_client.heat, self.stack_name,
                    rendered_template, merged_parameters, env_file)
            except heat.exc.StackFailure as err:
                self.stack_id = err.args[0]
                raise
        else:
            self.stack_id = heat.get_id_with_name(self.openstack_client.heat,
                                                  self.stack_name)

        # get info about deployed objects
        outputs = heat.get_stack_outputs(self.openstack_client.heat,
                                         self.stack_id)
        override = self._get_override(specification.get('override'))

        agents = filter_agents(agents, outputs, override)

        if (not self.privileged_mode) and accommodation.get('density', 1) == 1:
            get_host_fn = functools.partial(nova.get_server_host_id,
                                            self.openstack_client.nova)
            agents = distribute_agents(agents, get_host_fn)

        return agents

    def _deploy_support_stacks(self, support_templates, base_dir):
        for stack in support_templates:
            try:
                support_name = stack['name']
                support_template = utils.read_file(stack['template'],
                                                   base_dir=base_dir)

                support_env_file = stack.get('env_file', None)
                if support_env_file is not None:
                    support_env_file = self._render_env_template(
                        support_env_file, base_dir)

                # user should set default values in supoort template
                # or provide a heat environment file to update
                # parameters for support templates
                support_template_params = {}

                support_id = heat.create_stack(
                    self.openstack_client.heat, support_name,
                    support_template, support_template_params,
                    support_env_file)

                # track support stacks for cleanup
                current_stack = self.TrackStack(name=support_name,
                                                id=support_id)
                self.support_stacks.append(current_stack)
                LOG.debug('Tracking support stacks: %s',
                          self.support_stacks)

            except heat.exc.Conflict as err:
                # continue even if support stack already exists. This
                # allows re-use of existing support stacks if multiple
                # runs reference the same support stack.
                LOG.info('Ignoring stack exists errors: %s', err)
                # clear the exception so polling heat later doesn't
                # continue to show the exception in the logs
                if sys.version_info < (3, 0):
                    sys.exc_clear()

            except heat.exc.StackFailure as err:
                self.stackid = err.args[0]
                raise

    def _get_override(self, override_spec):
        def override_ip(agent, ip_type):
            return dict(ip=nova.get_server_ip(
                self.openstack_client.nova, agent['id'], ip_type))

        if override_spec:
            if override_spec.get('ip'):
                return functools.partial(override_ip,
                                         ip_type=override_spec.get('ip'))

    # translate jinja decorations in env files
    def _render_env_template(self, env_file, base_dir):
        env_template = utils.read_file(env_file,
                                       base_dir=base_dir)
        env_values = {
            'CONF': cfg.CONF,
            'unique': self.stack_name
        }
        compiled_env = jinja2.Template(env_template)
        rendered_env = compiled_env.render(env_values)

        environment = utils.read_yaml(rendered_env)

        return environment

    #def deploy(self, deployment, base_dir=None, server_endpoint=None):
    def deploy(self, deployment, base_dir=None):
        agents = {}

        if not deployment:
            # local mode, create fake agent
            agents.update(dict(local=dict(id='local', mode='alone',
                                          node='localhost')))

        if deployment.get('template'):
            if not self.openstack_client:
                raise DeploymentException(
                    'OpenStack client is not initialized. '
                    'Template-based deployment is ignored.')
            else:
                # deploy topology specified by HOT
                agents.update(self._deploy_from_hot(
                    #deployment, server_endpoint, base_dir=base_dir))
                    deployment, base_dir=base_dir))

        if not agents:
            print("No VM Deployed - Deploy")
            raise Exception('No agents deployed.')

        if deployment.get('agents'):
            # agents are specified statically
            agents.update(dict((a['id'], a) for a in deployment.get('agents')))

        return agents

    def cleanup(self):
        # cleanup the test stack first since it could be referencing resources
        # in a support stack, and it was the last stack created.
        if self.stack_id is not None and cfg.CONF.cleanup_on_exit:
            LOG.debug('Cleaning up the test stack: %s with id: %s',
                      self.stack_name, self.stack_id)
            heat.wait_stack_deletion(self.openstack_client.heat, self.stack_id)

        # cleanup support stacks; reverse the order to prevent deletion errors
        # due to possible dependencies between the support stacks.
        # Only stacks tracked during the run are deleted. e.g. if a support
        # stack already existed, the current run doesn't "own" it so it
        # won't be cleaned up.
        if len(self.support_stacks) > 0 and cfg.CONF.cleanup_on_exit:
            for stack in reversed(self.support_stacks):
                LOG.debug('Cleaning up the support stack: %s with id: %s',
                          stack.name, stack.id)
                heat.wait_stack_deletion(self.openstack_client.heat, stack.id)

def read_scenario(scenario_name):
    scenario_file_name = scenario_name
    LOG.debug('Scenario %s is resolved to %s', scenario_name,
              scenario_file_name)

    scenario = utils.read_yaml_file(scenario_file_name)

    schema = utils.read_yaml_file(utils.resolve_relative_path(
        '%s%s.yaml' % (config.SCHEMAS, 'scenario')))
    utils.validate_yaml(scenario, schema)

    scenario['title'] = scenario.get('title') or scenario_file_name
    scenario['file_name'] = scenario_file_name

    return scenario

def _under_openstack():
    required = ['os_username', 'os_password', 'os_auth_url']
    for param in required:
        if param not in cfg.CONF:
            return False
        if not cfg.CONF.get(param):
            return False
    return True

def _extend_agents(agents_map):
    extended_agents = {}
    for agent in agents_map.values():
        extended = copy.deepcopy(agent)
        if agent.get('slave_id'):
            extended['slave'] = copy.deepcopy(agents_map[agent['slave_id']])
        if agent.get('master_id'):
            extended['master'] = copy.deepcopy(agents_map[agent['master_id']])
        extended_agents[agent['id']] = extended
    return extended_agents

def play_scenario(scenario):
    deployment = None
    output = dict(scenarios={}, agents={})
    output['scenarios'][scenario['title']] = scenario

    try:
        deployment = Deployment()

        if _under_openstack():
            openstack_params = utils.pack_openstack_params(cfg.CONF)
            try:
                deployment.connect_to_openstack(
                    openstack_params, cfg.CONF.flavor_name,
                    cfg.CONF.image_name, cfg.CONF.external_net,
                    cfg.CONF.dns_nameservers)
#            except .OpenStackClientException:
#                raise
            except Exception as e:
                LOG.warning('Failed to connect to OpenStack: %s. Please '
                            'verify parameters: %s', e, openstack_params)
                # try to proceed even if OpenStack connection fails
                # (in case scenario does not need it)

        base_dir = os.path.dirname(scenario['file_name'])
        scenario_deployment = scenario.get('deployment', {})
        #server_endpoint = (cfg.CONF.server_endpoint
        #                   if 'server_endpoint' in cfg.CONF else "10.10.100.100")

        #agents = deployment.deploy(scenario_deployment, base_dir=base_dir,
        agents = deployment.deploy(scenario_deployment, base_dir=base_dir)
        #                           server_endpoint=server_endpoint)

        if not agents:
            print("No VM Deployed - Play-Scenario")
            raise Exception('No agents deployed.')

        agents = _extend_agents(agents)
        output['agents'] = agents
        LOG.debug('Deployed agents: %s', agents)
        print(agents)

        if not agents:
            raise Exception('No agents deployed.')

    except BaseException as e:
        if isinstance(e, KeyboardInterrupt):
            LOG.info('Caught SIGINT. Terminating')
            record = dict(id=utils.make_record_id(), status='interrupted')
        else:
            error_msg = 'Error while executing scenario: %s' % e
            LOG.exception(e)
#    finally:
#        if deployment:
#            try:
#                deployment.cleanup()
#            except Exception as e:
#                LOG.error('Failed to cleanup the deployment: %s', e,
#                          exc_info=True)
    return output

def act():
    # outputs = []
    for scenario_name in cfg.CONF.scenario:
        LOG.info('Play scenario: %s', scenario_name)
        print('Play scenario: %s', scenario_name)


        scenario = read_scenario(scenario_name)

        play_output = play_scenario(scenario)
        print(play_output)
        return play_output
#        outputs.append(copy.deepcopy(play_output))

def create_vsperf_conffile(agents, tgen):
        # define the timestamp
    date = datetime.datetime.fromtimestamp(time.time())
    timestamp = date.strftime('%Y-%m-%d_%H-%M-%S')
    if not len(agents):
        print("No agents provided")
        return
    filename = 'vsperf-'+ tgen + '.conf'
    filepath = os.path.join('./testconfs', filename)
    destfilename = 'vsperf-' + tgen + "-" + timestamp + ".conf"
    destfilepath = os.path.join('./testconfs', destfilename)
    shutil.copyfile(filepath, destfilepath) 
    print('Using File: ', destfilepath)
    east_chassis_ip = agents[0]['public_ip']
    east_data_ip = agents[0]['private_ip']
    if len(agents) == 2:
        west_chassis_ip = agents[1]['public_ip']
        west_data_ip = agents[1]['private_ip']
    else:
        west_chassis_ip = east_chassis_ip
        west_data_ip = east_chassis_ip
    with open(destfilepath, "+a") as filep:
        if tgen == "spirent":
            filep.write('TRAFFICGEN_STC_EAST_CHASSIS_ADDR = "{0}" \n'.format(east_chassis_ip))
            filep.write('TRAFFICGEN_STC_WEST_CHASSIS_ADDR = "{0}" \n'.format(west_chassis_ip))
        if tgen == "ixnet":
            filep.writelines("TRAFFICGEN_EAST_IXIA_HOST = " +
                        east_chassis_ip + '\n')
            filep.writelines("TRAFFICGEN_WEST_IXIA_HOST = " +
                        west_chassis_ip + '\n')
        

def main():
    output = collections.defaultdict()
    utils.init_config_and_logging(
        config.COMMON_OPTS + config.OPENSTACK_OPTS +
        config.SERVER_OPTS
    )
    print(cfg.CONF.image_name)
    print(cfg.CONF.flavor_name)
    print(cfg.CONF.external_net)
    print(cfg.CONF.dns_nameservers)
    print(cfg.CONF.scenario)
#    output = act()
    output={'agents':[]}
    list_of_agents = [{'private_ip': '20.0.0.5', 'public_ip': '10.10.105.33', 'compute_node': 'pod10-node4', 'name': 'testvnf_ktaefo_slave_0'}, {'private_ip': '20.0.0.8', 'public_ip': '10.10.105.62', 'compute_node': 'pod10-node5', 'name': 'testvnf_ktaefo_master_0'}]
#    list_of_agents = []
    #output = {'agents': {'testvnf_emtkia_slave_0': {'mode': 'slave', 'availability_zone': 'nova:pod10-node4', 'master': {
    #    'mode': 'master', 'availability_zone': 'nova:pod10-node5', 'slave_id': 'testvnf_emtkia_slave_0', 'zone': 'nova', 'node': 'pod10-node5',
    #    'pip': '10.10.105.51', 'id': 'testvnf_emtkia_master_0', 'ip': '10.0.0.16'}, 'zone': 'nova', 'node': 'pod10-node4', 'pip': '10.10.105.34',
    #    'id': 'testvnf_emtkia_slave_0', 'ip': '10.0.0.6', 'master_id': 'testvnf_emtkia_master_0'}, 'testvnf_emtkia_master_0': {
    #        'mode': 'master', 'availability_zone': 'nova:pod10-node5', 'slave_id': 'testvnf_emtkia_slave_0', 'zone': 'nova', 'node': 'pod10-node5',
    #        'pip': '10.10.105.51', 'slave': {'mode': 'slave', 'availability_zone': 'nova:pod10-node4', 'zone': 'nova', 'node': 'pod10-node4',
    #                                         'pip': '10.10.105.34', 'id': 'testvnf_emtkia_slave_0', 'ip': '10.0.0.6', 'master_id': 'testvnf_emtkia_master_0'},
    #        'id': 'testvnf_emtkia_master_0', 'ip': '10.0.0.16'}}, 'scenarios': {'OpenStack L2 Performance': {'file_name':'templates/l2_2c_2i.yaml', 'deployment': {
    #            'template': 'l2pub.hot', 'accommodation': ['pair', 'single_room', {'compute_nodes': 2}]},
    #            'title': 'OpenStack L2 Performance', 'description': 'In this scenario tdep launches 1 pair of instances in the same tenant network. Each instance is hosted on a separate compute node. The traffic goes within the tenant network (L2 domain).'}}}
    for count in range (len(output['agents'])):
        ag_dict = collections.defaultdict()
        name = str(list(output['agents'].keys())[count])
        private_ip = output['agents'][name]['ip']
        public_ip = output['agents'][name]['pip']
        node = output['agents'][name]['node']
        list_of_agents.append({'name': name, 'private_ip': private_ip, 'public_ip': public_ip, 'compute_node': node})
    print(list_of_agents)
    create_vsperf_conffile(list_of_agents, cfg.CONF.trafficgen)

if __name__ == "__main__":
    print("hello")
    main()
