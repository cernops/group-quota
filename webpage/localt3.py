#!/usr/bin/python


import os.path
import os, sys
import cgitb

cgitb.enable()
os.environ['SCRIPT_NAME'] = os.path.basename(sys.argv[0])
os.environ['CFGFILE'] = "groupquota/localt3.cfg"

if __name__ == "__main__":

    import groupquota.main
    groupquota.main.render_page()
