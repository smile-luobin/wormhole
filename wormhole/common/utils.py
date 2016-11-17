"""Utilities and helper functions."""

import math
import crypt
import os
import random
import re
import tempfile

from wormhole import exception
from wormhole.i18n import _, _LE
from wormhole.common import jsonutils
from wormhole.common import processutils
from wormhole.common import excutils
from wormhole.common import strutils
from wormhole.common import units
from wormhole.common import timeutils
from wormhole.common import log as logging

from oslo_config import cfg

CONF = cfg.CONF

utils_opt = [
    cfg.BoolOpt('fake_execute',
                default=False,
                help='If passed, use fake network devices and addresses'),
]

CONF.register_opts(utils_opt)

LOG = logging.getLogger(__name__)


class UndoManager(object):
    """Provides a mechanism to facilitate rolling back a series of actions
    when an exception is raised.
    """

    def __init__(self):
        self.undo_stack = []

    def undo_with(self, undo_func):
        self.undo_stack.append(undo_func)

    def _rollback(self):
        for undo_func in reversed(self.undo_stack):
            undo_func()

    def rollback_and_reraise(self, msg=None, **kwargs):
        """Rollback a series of actions then re-raise the exception.

        .. note:: (sirp) This should only be called within an
                  exception handler.
        """
        with excutils.save_and_reraise_exception():
            if msg:
                LOG.exception(msg, **kwargs)

            self._rollback()


class SmarterEncoder(jsonutils.json.JSONEncoder):
    """Help for JSON encoding dict-like objects."""

    def default(self, obj):
        if not isinstance(obj, dict) and hasattr(obj, 'iteritems'):
            return dict(obj.iteritems())
        return super(SmarterEncoder, self).default(obj)


def utf8(value):
    """Try to turn a string into utf-8 if possible.

    Code is directly from the utf8 function in
    http://github.com/facebook/tornado/blob/master/tornado/escape.py

    """
    if isinstance(value, unicode):
        return value.encode('utf-8')
    assert isinstance(value, str)
    return value


def get_root_helper():
    pass
    # return 'sudo wormhole-api %s' % CONF.rootwrap_config


def execute(*cmd, **kwargs):
    """Convenience wrapper around oslo's execute() method."""
    if 'run_as_root' in kwargs and 'root_helper' not in kwargs:
        # kwargs['root_helper'] = get_root_helper()
        pass
    if CONF.fake_execute:
        LOG.debug('FAKE EXECUTE: %s', ' '.join(map(str, cmd)))
        return 'fake', 0
    else:
        return processutils.execute(*cmd, **kwargs)


def trycmd(*cmd, **kwargs):
    return processutils.trycmd(*cmd, **kwargs)


def _calculate_count(size_in_m, blocksize):
    # Check if volume_dd_blocksize is valid
    try:
        # Rule out zero-sized/negative/float dd blocksize which
        # cannot be caught by strutils
        if blocksize.startswith(('-', '0')) or '.' in blocksize:
            raise ValueError
        bs = strutils.string_to_bytes('%sB' % blocksize)
    except ValueError:
        msg = (_("Incorrect value error: %(blocksize)s, "
                 "it may indicate that \'volume_dd_blocksize\' "
                 "was configured incorrectly. Fall back to default.")
               % {'blocksize': blocksize})
        LOG.warn(msg)
        # Fall back to default blocksize
        CONF.clear_override('volume_dd_blocksize')
        blocksize = CONF.volume_dd_blocksize
        bs = strutils.string_to_bytes('%sB' % blocksize)

    print(size_in_m, units.Mi, bs)
    count = math.ceil(size_in_m * units.Mi / bs)

    return blocksize, int(count)


def check_for_odirect_support(src, dest, flag='oflag=direct'):
    # Check whether O_DIRECT is supported
    try:
        execute('dd', 'count=0', 'if=%s' % src, 'of=%s' % dest,
                flag, run_as_root=True)
        return True
    except processutils.ProcessExecutionError:
        return False


def copy_volume(srcstr, deststr, size_in_m, blocksize, sync=False, ionice=None):
    # Use O_DIRECT to avoid thrashing the system buffer cache
    extra_flags = []
    if check_for_odirect_support(srcstr, deststr, 'iflag=direct'):
        extra_flags.append('iflag=direct')

    if check_for_odirect_support(srcstr, deststr, 'oflag=direct'):
        extra_flags.append('oflag=direct')

    # If the volume is being unprovisioned then
    # request the data is persisted before returning,
    # so that it's not discarded from the cache.
    if sync and not extra_flags:
        extra_flags.append('conv=fdatasync')

    blocksize, count = _calculate_count(size_in_m, blocksize)

    cmd = ['dd', 'if=%s' % srcstr, 'of=%s' % deststr,
           'count=%d' % count, 'bs=%s' % blocksize]
    cmd.extend(extra_flags)

    if ionice is not None:
        cmd = ['ionice', ionice] + cmd

    # Perform the copy
    start_time = timeutils.utcnow()
    execute(*cmd, run_as_root=True)
    duration = timeutils.delta_seconds(start_time, timeutils.utcnow())

    # NOTE(jdg): use a default of 1, mostly for unit test, but in
    # some incredible event this is 0 (cirros image?) don't barf
    if duration < 1:
        duration = 1
    mbps = (size_in_m / duration)
    mesg = ("Volume copy details: src %(src)s, dest %(dest)s, "
            "size %(sz).2f MB, duration %(duration).2f sec")
    LOG.debug(mesg % {"src": srcstr,
                      "dest": deststr,
                      "sz": size_in_m,
                      "duration": duration})
    mesg = _("Volume copy %(size_in_m).2f MB at %(mbps).2f MB/s")
    LOG.info(mesg % {'size_in_m': size_in_m, 'mbps': mbps})


def _generate_salt():
    salt_set = ('abcdefghijklmnopqrstuvwxyz'
                'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
                '0123456789./')
    salt = 16 * ' '
    return ''.join([random.choice(salt_set) for c in salt])


def set_passwd(username, admin_passwd, passwd_data, shadow_data):
    """set the password for username to admin_passwd

    The passwd_file is not modified.  The shadow_file is updated.
    if the username is not found in both files, an exception is raised.

    :param username: the username
    :param encrypted_passwd: the  encrypted password
    :param passwd_file: path to the passwd file
    :param shadow_file: path to the shadow password file
    :returns: nothing
    :raises: exception.WormholeException(), IOError()

    """

    # encryption algo - id pairs for crypt()
    algos = {'SHA-512': '$6$', 'SHA-256': '$5$', 'MD5': '$1$', 'DES': ''}

    salt = _generate_salt()

    # crypt() depends on the underlying libc, and may not support all
    # forms of hash. We try md5 first. If we get only 13 characters back,
    # then the underlying crypt() didn't understand the '$n$salt' magic,
    # so we fall back to DES.
    # md5 is the default because it's widely supported. Although the
    # local crypt() might support stronger SHA, the target instance
    # might not.
    encrypted_passwd = crypt.crypt(admin_passwd, algos['MD5'] + salt)
    if len(encrypted_passwd) == 13:
        encrypted_passwd = crypt.crypt(admin_passwd, algos['DES'] + salt)

    p_file = passwd_data.split("\n")
    s_file = shadow_data.split("\n")

    # username MUST exist in passwd file or it's an error
    for entry in p_file:
        split_entry = entry.split(':')
        if split_entry[0] == username:
            break
    else:
        msg = _('User %(username)s not found in password file.')
        raise exception.WormholeException(msg % username)

    # update password in the shadow file.It's an error if the
    # the user doesn't exist.
    new_shadow = list()
    found = False
    for entry in s_file:
        split_entry = entry.split(':')
        if split_entry[0] == username:
            split_entry[1] = encrypted_passwd
            found = True
        new_entry = ':'.join(split_entry)
        new_shadow.append(new_entry)

    if not found:
        msg = _('User %(username)s not found in shadow file.')
        raise exception.WormholeException(msg % username)

    return "\n".join(new_shadow)


DEVICE_RE = re.compile(r'^x?[a-z]?d?[a-z]$')


def list_device():
    """
    Example returns:
      [
        { "name": "/dev/sde", "type": "disk", "size": "3G", "maj:min": "8:16" },
        { "name": "/dev/sdh", "type": "disk", "size": "4G", "maj:min": "8:48" }
      ]
    """
    filter_fields = ['name', 'type', 'maj:min', 'size']
    dev_out, _err = trycmd('lsblk', '-dn', '-o', ','.join(filter_fields))
    dev_list = []
    for dev in dev_out.strip().split('\n'):
        res = dev.split()
        name, disk_type = res[:2]
        if disk_type == 'disk' and not name.endswith('da') and DEVICE_RE.match(
                name):
            res[0] = "/dev/" + name
            dev_list.append(dict(zip(filter_fields, res)))
    LOG.debug("scan host devices: %s", dev_list)
    return dev_list


def echo_scsi_command(path, content):
    """Used to echo strings to scsi subsystem."""

    args = ["-a", path]
    kwargs = dict(process_input=content,
                  run_as_root=True)
    execute('tee', *args, **kwargs)


def flush_device_io(device):
    """This is used to flush any remaining IO in the buffers."""
    try:
        LOG.debug("Flushing IO for device %s", device)
        execute('blockdev', '--flushbufs', device, run_as_root=True)
    except Exception as exc:
        LOG.warning(_("Failed to flush IO buffers prior to removing "))


def remove_device(device):
    path = "/sys/block/%s/device/delete" % device.replace("/dev/", "")
    if os.path.exists(path):
        # flush any outstanding IO first
        flush_device_io(device)

        LOG.debug("Remove SCSI device %(device)s with %(path)s",
                  {'device': device, 'path': path})
        echo_scsi_command(path, "1")


def robust_file_write(directory, filename, data):
    """Robust file write.

    Use "write to temp file and rename" model for writing the
    persistence file.

    :param directory: Target directory to create a file.
    :param filename: File name to store specified data.
    :param data: String data.
    """
    tempname = None
    dirfd = None
    try:
        dirfd = os.open(directory, os.O_DIRECTORY)

        # write data to temporary file
        with tempfile.NamedTemporaryFile(prefix=filename,
                                         dir=directory,
                                         delete=False) as tf:
            tempname = tf.name
            tf.write(data.encode('utf-8'))
            tf.flush()
            os.fdatasync(tf.fileno())
            tf.close()

            # Fsync the directory to ensure the fact of the existence of
            # the temp file hits the disk.
            os.fsync(dirfd)
            # If destination file exists, it will be replaced silently.
            os.rename(tempname, os.path.join(directory, filename))
            # Fsync the directory to ensure the rename hits the disk.
            os.fsync(dirfd)
    except OSError:
        with excutils.save_and_reraise_exception():
            LOG.error(_LE("Failed to write persistence file: %(path)s."),
                      {'path': os.path.join(directory, filename)})
            if os.path.isfile(tempname):
                os.unlink(tempname)
    finally:
        if dirfd:
            os.close(dirfd)
