# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# Copyright 2011 Justin Santa Barbara
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""Utilities and helper functions."""

import contextlib
import functools
import inspect
import os
import pyclbr
import re
import shutil
import sys
import tempfile
import tenacity
import time

from eventlet import pools
import logging
import netaddr
from oslo_concurrency import lockutils
from oslo_concurrency import processutils
from oslo_config import cfg
from oslo_log import log
from oslo_utils import importutils
from oslo_utils import netutils
from oslo_utils.secretutils import md5
from oslo_utils import strutils
from oslo_utils import timeutils
import paramiko
from webob import exc


from manila.common import constants
from manila.db import api as db_api
from manila import exception
from manila.i18n import _


CONF = cfg.CONF
LOG = log.getLogger(__name__)
if getattr(CONF, 'debug', False):
    logging.getLogger("paramiko").setLevel(logging.DEBUG)

_ISO8601_TIME_FORMAT_SUBSECOND = '%Y-%m-%dT%H:%M:%S.%f'
_ISO8601_TIME_FORMAT = '%Y-%m-%dT%H:%M:%S'

synchronized = lockutils.synchronized_with_prefix('manila-')


def get_fingerprint(self):
    """Patch paramiko

    This method needs to be patched to allow paramiko to work under FIPS.
    Until the patch to do this merges, patch paramiko here.

    TODO(carloss) Remove this when paramiko is patched.
    See https://github.com/paramiko/paramiko/pull/1928
    """
    return md5(self.asbytes(), usedforsecurity=False).digest()


paramiko.pkey.PKey.get_fingerprint = get_fingerprint


def isotime(at=None, subsecond=False):
    """Stringify time in ISO 8601 format."""

    # Python provides a similar instance method for datetime.datetime objects
    # called isoformat(). The format of the strings generated by isoformat()
    # have a couple of problems:
    # 1) The strings generated by isotime are used in tokens and other public
    #    APIs that we can't change without a deprecation period. The strings
    #    generated by isoformat are not the same format, so we can't just
    #    change to it.
    # 2) The strings generated by isoformat do not include the microseconds if
    #    the value happens to be 0. This will likely show up as random failures
    #    as parsers may be written to always expect microseconds, and it will
    #    parse correctly most of the time.

    if not at:
        at = timeutils.utcnow()
    st = at.strftime(_ISO8601_TIME_FORMAT
                     if not subsecond
                     else _ISO8601_TIME_FORMAT_SUBSECOND)
    tz = at.tzinfo.tzname(None) if at.tzinfo else 'UTC'
    # Need to handle either iso8601 or python UTC format
    st += ('Z' if tz in ['UTC', 'UTC+00:00'] else tz)
    return st


def _get_root_helper():
    return 'sudo manila-rootwrap %s' % CONF.rootwrap_config


def execute(*cmd, **kwargs):
    """Convenience wrapper around oslo's execute() function."""
    kwargs.setdefault('root_helper', _get_root_helper())
    if getattr(CONF, 'debug', False):
        kwargs['loglevel'] = logging.DEBUG
    return processutils.execute(*cmd, **kwargs)


class SSHPool(pools.Pool):
    """A simple eventlet pool to hold ssh connections."""

    def __init__(self, ip, port, conn_timeout, login, password=None,
                 privatekey=None, *args, **kwargs):
        self.ip = ip
        self.port = port
        self.login = login
        self.password = password
        self.conn_timeout = conn_timeout if conn_timeout else None
        self.path_to_private_key = privatekey
        super(SSHPool, self).__init__(*args, **kwargs)

    def create(self):  # pylint: disable=method-hidden
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        look_for_keys = True
        if self.path_to_private_key:
            self.path_to_private_key = os.path.expanduser(
                self.path_to_private_key)
            look_for_keys = False
        elif self.password:
            look_for_keys = False
        try:
            LOG.debug("ssh.connect: ip: %s, port: %s, look_for_keys: %s, "
                      "timeout: %s, banner_timeout: %s",
                      self.ip,
                      self.port,
                      look_for_keys,
                      self.conn_timeout,
                      self.conn_timeout)
            ssh.connect(self.ip,
                        port=self.port,
                        username=self.login,
                        password=self.password,
                        key_filename=self.path_to_private_key,
                        look_for_keys=look_for_keys,
                        timeout=self.conn_timeout,
                        banner_timeout=self.conn_timeout)
            if self.conn_timeout:
                transport = ssh.get_transport()
                transport.set_keepalive(self.conn_timeout)
            return ssh
        except Exception as e:
            msg = _("Check whether private key or password are correctly "
                    "set. Error connecting via ssh: %s") % e
            LOG.error(msg)
            raise exception.SSHException(msg)

    def get(self):
        """Return an item from the pool, when one is available.

        This may cause the calling greenthread to block. Check if a
        connection is active before returning it. For dead connections
        create and return a new connection.
        """
        if self.free_items:
            conn = self.free_items.popleft()
            if conn:
                if conn.get_transport().is_active():
                    return conn
                else:
                    conn.close()
            return self.create()
        if self.current_size < self.max_size:
            created = self.create()
            self.current_size += 1
            return created
        return self.channel.get()

    def remove(self, ssh):
        """Close an ssh client and remove it from free_items."""
        ssh.close()
        if ssh in self.free_items:
            self.free_items.remove(ssh)
            if self.current_size > 0:
                self.current_size -= 1


def check_ssh_injection(cmd_list):
    ssh_injection_pattern = ['`', '$', '|', '||', ';', '&', '&&', '>', '>>',
                             '<']

    # Check whether injection attacks exist
    for arg in cmd_list:
        arg = arg.strip()

        # Check for matching quotes on the ends
        is_quoted = re.match('^(?P<quote>[\'"])(?P<quoted>.*)(?P=quote)$', arg)
        if is_quoted:
            # Check for unescaped quotes within the quoted argument
            quoted = is_quoted.group('quoted')
            if quoted:
                if (re.match('[\'"]', quoted) or
                        re.search('[^\\\\][\'"]', quoted)):
                    raise exception.SSHInjectionThreat(command=cmd_list)
        else:
            # We only allow spaces within quoted arguments, and that
            # is the only special character allowed within quotes
            if len(arg.split()) > 1:
                raise exception.SSHInjectionThreat(command=cmd_list)

        # Second, check whether danger character in command. So the shell
        # special operator must be a single argument.
        for c in ssh_injection_pattern:
            if c not in arg:
                continue

            result = arg.find(c)
            if not result == -1:
                if result == 0 or not arg[result - 1] == '\\':
                    raise exception.SSHInjectionThreat(command=cmd_list)


class LazyPluggable(object):
    """A pluggable backend loaded lazily based on some value."""

    def __init__(self, pivot, **backends):
        self.__backends = backends
        self.__pivot = pivot
        self.__backend = None

    def __get_backend(self):
        if not self.__backend:
            backend_name = CONF[self.__pivot]
            if backend_name not in self.__backends:
                raise exception.Error(_('Invalid backend: %s') % backend_name)

            backend = self.__backends[backend_name]
            if isinstance(backend, tuple):
                name = backend[0]
                fromlist = backend[1]
            else:
                name = backend
                fromlist = backend

            self.__backend = __import__(name, None, None, fromlist)
            LOG.debug('backend %s', self.__backend)
        return self.__backend

    def __getattr__(self, key):
        backend = self.__get_backend()
        return getattr(backend, key)


def monkey_patch():
    """Patch decorator.

    If the Flags.monkey_patch set as True,
    this function patches a decorator
    for all functions in specified modules.
    You can set decorators for each modules
    using CONF.monkey_patch_modules.
    The format is "Module path:Decorator function".
    Example: 'manila.api.ec2.cloud:' \
     manila.openstack.common.notifier.api.notify_decorator'

    Parameters of the decorator is as follows.
    (See manila.openstack.common.notifier.api.notify_decorator)

    name - name of the function
    function - object of the function
    """
    # If CONF.monkey_patch is not True, this function do nothing.
    if not CONF.monkey_patch:
        return
    # Get list of modules and decorators
    for module_and_decorator in CONF.monkey_patch_modules:
        module, decorator_name = module_and_decorator.split(':')
        # import decorator function
        decorator = importutils.import_class(decorator_name)
        __import__(module)
        # Retrieve module information using pyclbr
        module_data = pyclbr.readmodule_ex(module)
        for key in module_data.keys():
            # set the decorator for the class methods
            if isinstance(module_data[key], pyclbr.Class):
                clz = importutils.import_class("%s.%s" % (module, key))
                # NOTE(vponomaryov): we need to distinguish class methods types
                # for py2 and py3, because the concept of 'unbound methods' has
                # been removed from the python3.x
                member_type = inspect.isfunction
                for method, func in inspect.getmembers(clz, member_type):
                    setattr(
                        clz, method,
                        decorator("%s.%s.%s" % (module, key, method), func))
            # set the decorator for the function
            if isinstance(module_data[key], pyclbr.Function):
                func = importutils.import_class("%s.%s" % (module, key))
                setattr(sys.modules[module], key,
                        decorator("%s.%s" % (module, key), func))


def file_open(*args, **kwargs):
    """Open file

    see built-in open() documentation for more details

    Note: The reason this is kept in a separate module is to easily
          be able to provide a stub module that doesn't alter system
          state at all (for unit tests)
    """
    return open(*args, **kwargs)


def service_is_up(service):
    """Check whether a service is up based on last heartbeat."""
    last_heartbeat = service['updated_at'] or service['created_at']
    # Timestamps in DB are UTC.
    tdelta = timeutils.utcnow() - last_heartbeat
    elapsed = tdelta.total_seconds()
    return abs(elapsed) <= CONF.service_down_time


def validate_service_host(context, host):
    service = db_api.service_get_by_host_and_topic(context, host,
                                                   'manila-share')
    if not service_is_up(service):
        raise exception.ServiceIsDown(service=service['host'])

    return service


@contextlib.contextmanager
def tempdir(**kwargs):
    tmpdir = tempfile.mkdtemp(**kwargs)
    try:
        yield tmpdir
    finally:
        try:
            shutil.rmtree(tmpdir)
        except OSError as e:
            LOG.debug('Could not remove tmpdir: %s', e)


def walk_class_hierarchy(clazz, encountered=None):
    """Walk class hierarchy, yielding most derived classes first."""
    if not encountered:
        encountered = []
    for subclass in clazz.__subclasses__():
        if subclass not in encountered:
            encountered.append(subclass)
            # drill down to leaves first
            for subsubclass in walk_class_hierarchy(subclass, encountered):
                yield subsubclass
            yield subclass


def cidr_to_network(cidr):
    """Convert cidr to network."""
    try:
        network = netaddr.IPNetwork(cidr)
        return network
    except netaddr.AddrFormatError:
        raise exception.InvalidInput(_("Invalid cidr supplied %s") % cidr)


def cidr_to_netmask(cidr):
    """Convert cidr to netmask."""
    return str(cidr_to_network(cidr).netmask)


def cidr_to_prefixlen(cidr):
    """Convert cidr to prefix length."""
    return cidr_to_network(cidr).prefixlen


def is_valid_ip_address(ip_address, ip_version):
    ip_version = ([int(ip_version)] if not isinstance(ip_version, list)
                  else ip_version)

    if not set(ip_version).issubset(set([4, 6])):
        raise exception.ManilaException(
            _("Provided improper IP version '%s'.") % ip_version)

    if not isinstance(ip_address, str):
        return False

    if 4 in ip_version:
        if netutils.is_valid_ipv4(ip_address):
            return True
    if 6 in ip_version:
        if netutils.is_valid_ipv6(ip_address):
            return True

    return False


def get_bool_param(param_string, params, default=False):
    param = params.get(param_string, default)
    if not strutils.is_valid_boolstr(param):
        msg = _("Value '%(param)s' for '%(param_string)s' is not "
                "a boolean.") % {'param': param, 'param_string': param_string}
        raise exception.InvalidParameterValue(err=msg)

    return strutils.bool_from_string(param, strict=True)


def is_all_tenants(search_opts):
    """Checks to see if the all_tenants flag is in search_opts

    :param dict search_opts: The search options for a request
    :returns: boolean indicating if all_tenants are being requested or not
    """
    all_tenants = search_opts.get('all_tenants')
    if all_tenants:
        try:
            all_tenants = strutils.bool_from_string(all_tenants, True)
        except ValueError as err:
            raise exception.InvalidInput(str(err))
    else:
        # The empty string is considered enabling all_tenants
        all_tenants = 'all_tenants' in search_opts
    return all_tenants


class IsAMatcher(object):
    def __init__(self, expected_value=None):
        self.expected_value = expected_value

    def __eq__(self, actual_value):
        return isinstance(actual_value, self.expected_value)


class ComparableMixin(object):
    def _compare(self, other, method):
        try:
            return method(self._cmpkey(), other._cmpkey())
        except (AttributeError, TypeError):
            # _cmpkey not implemented, or return different type,
            # so I can't compare with "other".
            return NotImplemented

    def __lt__(self, other):
        return self._compare(other, lambda s, o: s < o)

    def __le__(self, other):
        return self._compare(other, lambda s, o: s <= o)

    def __eq__(self, other):
        return self._compare(other, lambda s, o: s == o)

    def __ge__(self, other):
        return self._compare(other, lambda s, o: s >= o)

    def __gt__(self, other):
        return self._compare(other, lambda s, o: s > o)

    def __ne__(self, other):
        return self._compare(other, lambda s, o: s != o)


class retry_if_exit_code(tenacity.retry_if_exception):
    """Retry on ProcessExecutionError specific exit codes."""
    def __init__(self, codes):
        self.codes = (codes,) if isinstance(codes, int) else codes
        super(retry_if_exit_code, self).__init__(self._check_exit_code)

    def _check_exit_code(self, exc):
        return (exc and isinstance(exc, processutils.ProcessExecutionError) and
                exc.exit_code in self.codes)


def retry(retry_param=Exception,
          interval=1,
          retries=10,
          backoff_rate=2,
          backoff_sleep_max=None,
          wait_random=False,
          infinite=False,
          retry=tenacity.retry_if_exception_type):

    if retries < 1:
        raise ValueError('Retries must be greater than or '
                         'equal to 1 (received: %s). ' % retries)

    if wait_random:
        kwargs = {'multiplier': interval}
        if backoff_sleep_max is not None:
            kwargs.update({'max': backoff_sleep_max})
        wait = tenacity.wait_random_exponential(**kwargs)
    else:
        kwargs = {'multiplier': interval, 'min': 0, 'exp_base': backoff_rate}
        if backoff_sleep_max is not None:
            kwargs.update({'max': backoff_sleep_max})
        wait = tenacity.wait_exponential(**kwargs)

    if infinite:
        stop = tenacity.stop.stop_never
    else:
        stop = tenacity.stop_after_attempt(retries)

    def _decorator(f):

        @functools.wraps(f)
        def _wrapper(*args, **kwargs):
            r = tenacity.Retrying(
                sleep=tenacity.nap.sleep,
                before_sleep=tenacity.before_sleep_log(LOG, logging.DEBUG),
                after=tenacity.after_log(LOG, logging.DEBUG),
                stop=stop,
                reraise=True,
                retry=retry(retry_param),
                wait=wait)
            return r(f, *args, **kwargs)

        return _wrapper

    return _decorator


def get_bool_from_api_params(key, params, default=False, strict=True):
    """Parse bool value from request params.

    HTTPBadRequest will be directly raised either of the cases below:
    1. invalid bool string was found by key(with strict on).
    2. key not found while default value is invalid(with strict on).
    """
    param = params.get(key, default)
    try:
        param = strutils.bool_from_string(param,
                                          strict=strict,
                                          default=default)
    except ValueError:
        msg = _('Invalid value %(param)s for %(param_string)s. '
                'Expecting a boolean.') % {'param': param,
                                           'param_string': key}
        raise exc.HTTPBadRequest(explanation=msg)
    return param


def check_params_exist(keys, params):
    """Validates if keys exist in params.

    :param keys: List of keys to check
    :param params: Parameters received from REST API
    """
    if any(set(keys) - set(params)):
        msg = _("Must specify all mandatory parameters: %s") % keys
        raise exc.HTTPBadRequest(explanation=msg)


def check_params_are_boolean(keys, params, default=False):
    """Validates if keys in params are boolean.

    :param keys: List of keys to check
    :param params: Parameters received from REST API
    :param default: default value when it does not exist
    :return: a dictionary with keys and respective retrieved value
    """
    result = {}
    for key in keys:
        value = get_bool_from_api_params(key, params, default, strict=True)
        result[key] = value
    return result


def require_driver_initialized(func):
    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        # we can't do anything if the driver didn't init
        if not self.driver.initialized:
            driver_name = self.driver.__class__.__name__
            raise exception.DriverNotInitialized(driver=driver_name)
        return func(self, *args, **kwargs)
    return wrapper


def convert_str(text):
    """Convert to native string.

    Convert bytes and Unicode strings to native strings:

    * convert to Unicode on Python 3: decode bytes from UTF-8
    """
    if isinstance(text, bytes):
        return text.decode('utf-8')
    else:
        return text


def translate_string_size_to_float(string, multiplier='G'):
    """Translates human-readable storage size to float value.

    Supported values for 'multiplier' are following:
        K - kilo | 1
        M - mega | 1024
        G - giga | 1024 * 1024
        T - tera | 1024 * 1024 * 1024
        P = peta | 1024 * 1024 * 1024 * 1024

    returns:
        - float if correct input data provided
        - None if incorrect
    """
    if not isinstance(string, str):
        return None
    multipliers = ('K', 'M', 'G', 'T', 'P')
    mapping = {
        k: 1024.0 ** v
        for k, v in zip(multipliers, range(len(multipliers)))
    }
    if multiplier not in multipliers:
        raise exception.ManilaException(
            "'multiplier' arg should be one of following: "
            "'%(multipliers)s'. But it is '%(multiplier)s'." % {
                'multiplier': multiplier,
                'multipliers': "', '".join(multipliers),
            }
        )
    try:
        value = float(string.replace(",", ".")) / 1024.0
        value = value / mapping[multiplier]
        return value
    except (ValueError, TypeError):
        matched = re.match(
            r"^(\d*[.,]*\d*)([%s])$" % ''.join(multipliers), string)
        if matched:
            # The replace() is needed in case decimal separator is a comma
            value = float(matched.groups()[0].replace(",", "."))
            multiplier = mapping[matched.groups()[1]] / mapping[multiplier]
            return value * multiplier


def wait_for_access_update(context, db, share_instance,
                           migration_wait_access_rules_timeout):
    starttime = time.time()
    deadline = starttime + migration_wait_access_rules_timeout
    tries = 0

    while True:
        instance = db.share_instance_get(context, share_instance['id'])

        if instance['access_rules_status'] == constants.STATUS_ACTIVE:
            break

        tries += 1
        now = time.time()
        if (instance['access_rules_status'] ==
                constants.SHARE_INSTANCE_RULES_ERROR):
            msg = _("Failed to update access rules"
                    " on share instance %s") % share_instance['id']
            raise exception.ShareMigrationFailed(reason=msg)
        elif now > deadline:
            msg = _("Timeout trying to update access rules"
                    " on share instance %(share_id)s. Timeout "
                    "was %(timeout)s seconds.") % {
                'share_id': share_instance['id'],
                'timeout': migration_wait_access_rules_timeout}
            raise exception.ShareMigrationFailed(reason=msg)
        else:
            # 1.414 = square-root of 2
            time.sleep(1.414 ** tries)


class DoNothing(str):
    """Class that literrally does nothing.

    We inherit from str in case it's called with json.dumps.
    """

    def __call__(self, *args, **kwargs):
        return self

    def __getattr__(self, name):
        return self


DO_NOTHING = DoNothing()


def notifications_enabled(conf):
    """Check if oslo notifications are enabled."""
    notifications_driver = set(conf.oslo_messaging_notifications.driver)
    return notifications_driver and notifications_driver != {'noop'}


def if_notifications_enabled(function):
    """Calls decorated method only if notifications are enabled."""
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        if notifications_enabled(CONF):
            return function(*args, **kwargs)
        return DO_NOTHING
    return wrapped


def write_remote_file(ssh, filename, contents, as_root=False):
    tmp_filename = "%s.tmp" % filename
    if as_root:
        cmd = 'sudo tee "%s" > /dev/null' % tmp_filename
        cmd2 = 'sudo mv -f "%s" "%s"' % (tmp_filename, filename)
    else:
        cmd = 'cat > "%s"' % tmp_filename
        cmd2 = 'mv -f "%s" "%s"' % (tmp_filename, filename)
    stdin, __, __ = ssh.exec_command(cmd)
    stdin.write(contents)
    stdin.close()
    stdin.channel.shutdown_write()
    ssh.exec_command(cmd2)
