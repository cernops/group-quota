#!/usr/bin/python
# Desc: This script checks, being very paranoid, for differences between the
#       database holding the atlas group quota information (on database) and the
#       current file held in 'QUOTA_FILE' below.  If there are any differences,
#       it backs up 'QUOTA_FILE' to 'QUOTA_BACK' and writes the changed quotas
#       to a temporary file before replacing the real 'QUOTA_FILE' with the
#       temporary one.  This is run as cronjob every 10 minutes
#
# By: William Strecker-Kellogg -- willsk@bnl.gov
#
# CHANGELOG:
#   8/10/10     v1.0 to be put into production
#   8/16/10     v1.5 email linux farm when changes are made
#   4/07/11     v2.0 email is configurable on the command line
#   6/15/11     v2.5 make reconfig optional, use logging and tempfile
#   1/25/12     v3.0 rewrite for heirarchical groups -- for 7.6.X upgrade
#   5/29/15     v4.0 rewrite to use unified group-class tree interface

# NOTE: This script works in conjuction with farmweb01:/var/www/cgi-bin/group_quota.py
#       and the farmweb01:/var/www/public/cronjobs/update_db_condor_usage.py, which
#       act as a database frontend and update the busy slots in each group respectively

import logging
import optparse
import os
import os.path
import shutil
import smtplib
import subprocess
import sys
import tempfile
import time

import gq.group as group
import gq.group.db as gdb
import gq.group.file as gfile
from gq.log import setup_logging

from email.MIMEText import MIMEText
from email.utils import formatdate

CONDOR_RECONFIG = '/usr/sbin/condor_reconfig'

QUOTA_FILE = '/tmp/atlas-group-definitions'
QUOTA_BACK = '/tmp/atlas-group-definitions.previous'
LOGFILE = '/tmp/group-def.log'

log = setup_logging(None, backup=3, size_mb=40, level=logging.DEBUG)

# Needed to run reconfig command below
os.environ['EXTRA_CFG_D'] = '/etc/condor/atlas.d/'


class UpdateQuotaGroup(group.QuotaGroup):

    def get_diff_str(self, other):
        mine = set([x.full_name for x in self.all()])
        theirs = set([x.full_name for x in other.all()])

        grps_added = theirs - mine
        grps_removed = mine - theirs

        s = ''

        for grp in grps_added:
            s += "Added " + repr(other.find(grp)) + "\n"
        for grp in grps_removed:
            s += "Deleted " + repr(self.find(grp)) + "\n"

        for grpname in mine & theirs:
            mygrp = self.find(grpname)
            theirgrp = other.find(grpname)

            diffattrs = mygrp.diff(theirgrp)
            for attr, myval, theirval in ((x, getattr(mygrp, x), getattr(theirgrp, x))
                                          for x in diffattrs):
                s += "Group '%s' - %s changed from %s to %s\n" % (grpname, attr, myval, theirval)

        return s

    def __str__(self):

        msg = "GROUP_NAMES = %s\n" % ', '.join(x.full_name for x in self)
        for g in reversed(list(self)):
            msg += '\n'
            msg += 'GROUP_QUOTA_%s = %d\n' % (g.full_name, g.quota)
            msg += 'GROUP_PRIO_FACTOR_%s = %.1f\n' % (g.full_name, g.prio)
            msg += 'GROUP_ACCEPT_SURPLUS_%s = %s\n' % (g.full_name, g.surplus)
        return msg

    def write_file(self, fname):
        try:
            fobj = open(fname, "w")
        except EnvironmentError:
            log.exception("Error opening %s for writing", fname)
            sys.exit(1)
        try:
            header = """\
# /-----------------------------------------------------\\
# | This file is automatically generated -- DO NOT EDIT |
# | Last updated:     %s          |
# \-----------------------------------------------------/
""" % time.ctime()
            fobj.write(header + "\n")
            fobj.write(str(self))
            fobj.write("\n")
            fobj.close()
        except EnvironmentError:
            log.exception("Error writing to %s", fname)
            os.unlink(fname)
            sys.exit(1)


get_file_groups = lambda x: gfile.build_quota_groups_file(x, UpdateQuotaGroup)
get_db_groups = lambda: gdb.build_quota_groups_db(UpdateQuotaGroup)


def overwrite_file(groups):
    # 1. Open temporary file --> Write Changed quota file to temp file
    # 2. A bit pedantic, but re-scan temp file for consistency
    # 3. Overwrite backup file with copy of current file
    # 4. Replace current file with temp copy

    # Needs to be on same filesystem to allow hardlinking that goes on when
    # os.rename() is called, else we get a 'Invalid cross-device link' error
    tmpname = tempfile.mktemp(suffix='grpq', dir=os.path.dirname(QUOTA_FILE))
    groups.write_file(tmpname)

    # This may be overkill...but can't hurt -- reread tmpfile and compare w/ db
    new_groups = get_file_groups(tmpname)
    if not new_groups.full_cmp(groups):
        log.error("Very strange, new file %s is corrupt", tmpname)
        sys.exit(1)

    # Overwrite the backup with a simple copy operation
    if os.path.exists(QUOTA_FILE):
        shutil.copy2(QUOTA_FILE, QUOTA_BACK)

    # Replace (atomically) the actual file with the new version
    os.rename(tmpname, QUOTA_FILE)

    log.info('Quota file updated with new values')


def send_email(address, changes):

    log.info('Sending mail to "%s"...' % address)
    body = \
        """
Info: condor03 has detected a change in the ATLAS group quota
database; the changes that were made are:

%s

Receipt of this message indicates that condor has been successfully
reconfigured to use the new quotas indicated on the page above.
""" % changes.strip()
    msg = MIMEText(body)
    msg['From'] = "root@condor03"
    msg['To'] = address
    msg['Subject'] = "Group quotas changed"
    msg['Date'] = formatdate(localtime=True)
    try:
        smtp_server = smtplib.SMTP('rcf.rhic.bnl.gov', 25)
        smtp_server.sendmail(msg['From'], msg['To'], msg.as_string())
    except:
        log.error('Problem sending mail, no message sent')
        return 1
    return 0


def parse_options():
    parser = optparse.OptionParser()
    parser.add_option("-m", "--mail", action="store", dest="email",
                      help="Send email to address given here when a change is made")
    parser.add_option("-r", "--reconfig", action="store_true", default=False,
                      help="Issue a condor_reconfig after a change is detected")
    # No args!
    return parser.parse_args()[0]


if __name__ == '__main__':

    options = parse_options()

    db_groups = get_db_groups()
    fp_groups = get_file_groups(QUOTA_FILE)

    if db_groups.full_cmp(fp_groups):
        log.debug('No Database Change...')
        sys.exit(0)

    # Write the DB groups to the file
    overwrite_file(db_groups)

    changes = fp_groups.get_diff_str(db_groups)
    log.info('Changes made are:')
    for line in changes.split("\n"):
        if line:
            log.info(line)

    # Do a reconfig, and send mail before we exit
    if options.reconfig:
        if subprocess.call(CONDOR_RECONFIG) != 0:
            log.error('Problem with condor_reconfig, returned nonzero')
            sys.exit(1)
        log.info('Reconfig successful...')
    else:
        log.info('No reconfig done...')

    if options.email:
        sys.exit(send_email(options.email, changes))
    else:
        log.info('Not sending mail...')
