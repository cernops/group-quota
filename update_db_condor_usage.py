#!/usr/bin/python

# Desc: Simple script to read the condor usage information from the atlas central
#       manager and record it in the atlas_group_quota database on database (cronjob)
#
# By: William Strecker-Kellogg -- willsk@bnl.gov
#
# CHANGELOG:
# 7/29/10:  v1.0 put into production
# 8/3/10:   revised to not use RACF_Group, by using the hidden regexp() feature to match group names
# 10/29/10: revised to use only one call to condor_status, much more efficient
# 1/25/12:  Made code more streamlined and obvious

# NOTE: Needs the (empty) file /etc/condor/condor_config to exist!!
#       This script works in conjunction with /var/www/cgi-bin/group_quota.py on
#       farmweb01, which serves as a web interface to edit this database.

import sys
import MySQLdb
import datetime
import subprocess

try:
    conn = MySQLdb.connect(db="group_quotas", host="database.rcf.bnl.gov", user="atlas_update", passwd="XPASSX")
    dbc = conn.cursor()
except MySQLdb.Error, e:
    print "DB Error %d: %s" % (e.args[0], e.args[1])
    sys.exit(1)

dbc.execute("SELECT group_name FROM atlas_group_quotas")
db_groups = set(x[0] for x in dbc)
dbc.close()
if not db_groups:
    print 'Error, no groups in database?'
    sys.exit(1)

# get info from condor
proc = subprocess.Popen(["condor_status",  "-pool",  "condor03.usatlas.bnl.gov:9660",
                         "-constraint",  "AccountingGroup =!= UNDEFINED",
                         "-format",  "%s ",  "AccountingGroup", "-format",
                         "%s\\n", "Cpus"], stdout=subprocess.PIPE)

active = {}

for group,count in ((y.split()) for y in proc.communicate()[0].split("\n") if y):
    group = ".".join(group.split("@")[0].split(".")[:-1])
    if group in active:
        active[group] += int(count)
    elif group in db_groups:
        active[group] = int(count)
    else:
        print "Unknown group %s" % group

for group in (x for x in db_groups if x not in active):
    active[group] = 0

try:
    dbc = conn.cursor()
    for x in active:
        dbc.execute('UPDATE atlas_group_quotas SET busy = %d WHERE group_name = "%s"' % (active[x], x))
    dbc.execute('UPDATE atlas_group_quotas SET last_update = %s', datetime.datetime.now())
    dbc.close()
except MySQLdb.Error, e:
    print "DB Error %d: %s" % (e.args[0], e.args[1])
    conn.rollback()
    conn.close()
else:
    conn.commit()
    conn.close()
