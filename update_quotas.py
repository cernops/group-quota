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

# NOTE: This script works in conjuction with farmweb01:/var/www/cgi-bin/group_quota.py
#       and the farmweb01:/var/www/public/cronjobs/update_db_condor_usage.py, which
#       act as a database frontend and update the busy slots in each group respectively

import logging
import optparse
import os
import os.path
import re
import shutil
import smtplib
import subprocess
import sys
import tempfile
import time
from email.MIMEText import MIMEText
from email.utils import formatdate

import MySQLdb

DB_TABLE = "atlas_group_quotas"
DB_HOST = 'database.rcf.bnl.gov'
DB_USER = 'db_query'
DB_DB = 'group_quotas'

# TODO: CHANGE ME BACK!
QUOTA_FILE = '/etc/condor/atlas-group-definitions'
QUOTA_BACK = '/etc/condor/atlas-group-definitions.previous'
LOGFILE = '/etc/condor/group-def.log'
LOGLEVEL = logging.INFO

frmt = logging.Formatter("%(asctime)s %(name)s: (%(levelname)-6s)"
                         " %(message)s", "%m/%d %X")

log = logging.getLogger('quota_update')
log.setLevel(logging.DEBUG)

# Needed to run reconfig command below
os.environ['EXTRA_CFG_D'] = '/etc/condor/atlas.d/'


class Group(object):

    def __init__(self, name, quota=0, prio=10.0, surplus=False):
        if len(name) > 6 and name[:6] == "group_":
            self.name = name
        else:
            raise ValueError("Group names must start with 'group_'")

        self.quota = int(quota)
        self.prio = float(prio)
        self.surplus = str(bool(surplus)).upper()

    def __str__(self):
        msg = '\n'
        msg += 'GROUP_QUOTA_%s = %d\n' % (self.name, self.quota)
        msg += 'GROUP_PRIO_FACTOR_%s = %.1f\n' % (self.name, self.prio)
        msg += 'GROUP_ACCEPT_SURPLUS_%s = %s\n' % (self.name, self.surplus)
        return msg

    def diff(self, other):
        diffs = list()
        for x in ("name", "quota", "prio", "surplus"):
            if getattr(self, x) != getattr(other, x):
                diffs.append(x)
        return diffs

    def __repr__(self):
        return "'%s' - quota=%d - prio=%.1f - surplus=%s" % \
               (self.name, self.quota, self.prio, self.surplus)

    def __cmp__(self, other):

        same = True
        for x in ("name", "quota", "prio", "surplus"):
            if getattr(self, x) != getattr(other, x):
                same = False
                break
        if same:
            return 0
        else:
            if self.name >= other.name:
                return 1
            else:
                return -1


class Groups(object):
    def __init__(self):
        self._groups = {}

    def add_group(self, name, quota, prio, surplus):
        if name in self._groups:
            raise ValueError("Cannot have groups with duplicated name: %s" % name)
        else:
            self._groups[name] = Group(name, quota, prio, surplus)

    def check_tree(self):

        bad = set()
        for grp in sorted(self._groups, key=lambda x: len(x.split(".")), reverse=True):
            parent = ".".join(grp.split(".")[:-1])
            if parent and parent not in self._groups:
                bad.add(grp)
        return bad

    def __str__(self):
        x = "GROUP_NAMES = %s\n" % ', '.join(self)
        for grp in sorted(self):
            x += str(self._groups[grp])
        return x

    def __iter__(self):
        return iter(sorted(self._groups.keys()))

    def __getitem__(self, name):
        return self._groups[name]

    def __len__(self):
        return len(self._groups)

    def __eq__(self, other):
        if set(self._groups) ^ set(other._groups):
            return False
        else:
            for x in self:
                if not other[x] == self[x]:
                    return False
            return True

    def __ne__(self, other):
        return not self == other

    def diff(self, other):
        mine = set(self._groups)
        theirs = set(other._groups)

        grps_added = mine - theirs
        grps_removed = theirs - mine

        s = ''

        for grp in grps_added:
            s += "Added " + repr(self[grp]) + "\n"
        for grp in grps_removed:
            s += "Deleted '" + grp + "'\n"

        for grp in mine & theirs:
            diffattrs = self[grp].diff(other[grp])
            for attr, myval, theirval in ((x, getattr(self[grp], x), getattr(other[grp], x))
                                          for x in diffattrs):
                s += "Group '%s' - %s changed from %s to %s\n" % (grp, attr, myval, theirval)

        return s

    def write_file(self, fname):
        try:
            fobj = open(fname, "w")
        except IOError:
            log.exception("Error opening %s for writing", fname)
            sys.exit(1)
        try:
            fobj.write("# /-----------------------------------------------------\ \n")
            fobj.write("# | This file is automatically generated -- DO NOT EDIT | \n")
            fobj.write("# | Last updated:     " + time.ctime() + "          |\n")
            fobj.write("# \-----------------------------------------------------/ \n")
            fobj.write("\n")
            fobj.write(str(self))
            fobj.write("\n")
            fobj.close()
        except IOError:
            log.exception("Error writing to %s", fname)
            os.unlink(fname)
            sys.exit(1)


class DBGroups(Groups):

    def __init__(self, table, host=DB_HOST, user=DB_USER,
                 database=DB_DB):

        super(DBGroups, self).__init__()

        try:
            con = MySQLdb.connect(db=database, host=host, user=user, connect_timeout=3)
            dbc = con.cursor()
            dbc.execute("SELECT group_name, quota, priority, accept_surplus "
                        "FROM %s ORDER BY group_name" % table)
        except MySQLdb.Error, e:
            log.error("DB Error %d: %s" % (e.args[0], e.args[1]))
            sys.exit(1)
        else:
            for x in dbc:
                self.add_group(*x)
            dbc.close()
            con.close()

        if self.check_tree():
            log.error("Invalid group configuration found")
            sys.exit(1)


class FileGroups(Groups):

    def __init__(self, filename):

        super(FileGroups, self).__init__()

        try:
            fp = open(filename, "r")
        except IOError, e:
            log.error("Error opening %s: %s", filename, e)
            sys.exit(1)

        regexes = {
            "names": re.compile('^GROUP_NAMES\s*=\s*(.*)$'),
            "quota": re.compile('^GROUP_QUOTA_([\w\.]+)\s*=\s*(\d+)$'),
            "prio": re.compile('^GROUP_PRIO_FACTOR_([\w\.]+)\s*=\s*([\d\.]+)$'),
            "surplus": re.compile('^GROUP_ACCEPT_SURPLUS_([\w\.]+)\s*=\s*(\w+)$'),
            }

        grps = {}
        group_names = []
        for line in (x.strip() for x in fp if x and not re.match('^\s*#', x)):

            for kind, regex in regexes.items():
                if not regex.match(line):
                    continue
                if kind == "names":
                    group_names = regex.match(line).group(1).replace(' ', '').split(',')
                else:
                    grp, val = regex.match(line).groups()
                    if not grps.get(grp):
                        grps[grp] = {}
                    if kind == "surplus":
                        if val.upper() == "TRUE":
                            val = True
                        elif val.upper() == "FALSE":
                            val = False
                        else:
                            log.error("Invalid true/false value found: %s", val)
                            sys.exit(1)
                    grps[grp][kind] = val
        fp.close()
        for grp in group_names:
            p = grps.get(grp, None)
            if p is None or not ("quota" in p and "prio" in p and "surplus" in p):
                log.warning("Invalid incomplete group found: %s", grp)
                continue
            self.add_group(grp, p["quota"], p["prio"], p["surplus"])

        bad = self.check_tree()
        if bad:
            log.error("Missing dependencies for: %s", ", ".join(bad))
            sys.exit(1)


def send_email(address, changes):

    log.info('Sending mail to "%s"...' % address)
    body = \
        """
Info: condor03 has detected a change in the ATLAS group quota
database; the changes that were made are:

%s

Receipt of this message indicates that condor has been successfully
reconfigured to use the new quotas indicated on the page above.
""" % changes
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

# **************************************************************************************


parser = optparse.OptionParser()
parser.add_option("-m", "--mail", action="store", dest="email",
                  help="Send email to address given here when a change is made")
parser.add_option("-r", "--reconfig", action="store_true", default=False,
                  help="Issue a condor_reconfig after a change is detected")
options, args = parser.parse_args()

file_handler = logging.FileHandler(LOGFILE)
file_handler.setLevel(LOGLEVEL)
file_handler.setFormatter(frmt)
log.addHandler(file_handler)

db_groups = DBGroups(DB_TABLE)
fp_groups = FileGroups(QUOTA_FILE)

if db_groups == fp_groups:
    log.debug('No Database Change...')
    sys.exit(0)


# 1. Open temporary file --> Write Changed quota file to temp file
# 2. A bit pedantic, but re-scan temp file for consistency
# 3. Overwrite backup file with copy of current file
# 4. Replace current file with temp copy

# Needs to be on same filesystem to allow hardlinking that goes on when
# os.rename() is called, else we get a 'Invalid cross-device link' error
tmpname = tempfile.mktemp(suffix='grpq', dir=os.path.dirname(QUOTA_FILE))
db_groups.write_file(tmpname)

# This may be overkill...but can't hurt -- reread temp-file and compare w/ db
new_groups = FileGroups(tmpname)
if new_groups != db_groups:
    log.error("Very strange, new file %s is corrupt", tmpname)
    sys.exit(1)

# Overwrite the backup with a simply copy operation
shutil.copy2(QUOTA_FILE, QUOTA_BACK)

# Replace (atomically) the actual file with the new version
os.rename(tmpname, QUOTA_FILE)

log.info('Quota file updated with new values')

changes = db_groups.diff(fp_groups)
log.info('Changes made are:')
for line in (x for x in changes.split("\n") if x):
    log.info(line)

# Do a reconfig, and send mail before we exit
if options.reconfig:
    if subprocess.call('/usr/sbin/condor_reconfig') != 0:
        log.error('Problem with condor_reconfig, returned nonzero')
        sys.exit(1)
    log.info('Reconfig successful...')
else:
    log.info('No reconfig done...')

if options.email:
    sys.exit(send_email(options.email, changes))
else:
    log.info('Not sending mail...')

sys.exit(0)
