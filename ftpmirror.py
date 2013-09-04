#!/usr/bin/env python
"""
Mirrors a local directory tree to a server using plain FTP.

Features:
  the ability to exclude specific files and directories
  checking timestamp and file size to only upload changed files
  optional removal of orphaned files from the server

Copyright (C) 2013 David Osborn.
This program is released under the MIT license.

"""

import argparse
import calendar
import ftplib
import os
import os.path
import re
import sys
import time

os.stat_float_times(False)

################################################################################

# constants
BLOCK_SIZE = 32760
LOCAL_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))

################################################################################

# parse command-line arguments
parser = argparse.ArgumentParser(
	description=__doc__,
	formatter_class=argparse.RawDescriptionHelpFormatter)

parser.add_argument('host',     help='the domain name of the server')
parser.add_argument('user',     help='the name of the user to login as')
parser.add_argument('password', help='the password of the user to login as')
parser.add_argument('docroot',  help='the root directory of the tree on the server')
parser.add_argument('files',    help='the files to mirror', metavar='file', nargs='*')

parser.add_argument('-c', '--clean',   action='store_true', help='delete orphaned files on the server')
parser.add_argument('-x', '--exclude', action='append',     help='exclude files matching a regular expression', default=[], dest='excludes', metavar='re')
parser.add_argument('-k', '--keep',    action='append',     help='keep orphaned files matching a regular expression', default=[], dest='keeps', metavar='re')
parser.add_argument('-v', '--verbose', action='count',      help='print FTP commands', dest='verbosity')

args = parser.parse_args()

# always exclude .timestamp
args.excludes.append('.timestamp')

# ignore explicit files that match any of the "exclude" regular expressions
if args.files:
	for exclude in args.excludes:
		exclude = '(%s)(/|$)' % exclude
		for i in reversed(range(len(args.files))):
			file = args.files[i]
			if (re.match(exclude, file) or os.altsep and
				re.match(exclude, file.replace(os.sep, os.altsep))):
				print 'excluding %s' % file
				del args.files[i]
else:
	args.files = None

# force regular expressions to match the entire path
args.excludes = ['(%s)$' % exclude for exclude in args.excludes]
args.keeps = ['(%s)$' % keep for keep in args.keeps]

################################################################################

# initialize FTP connection
ftp = ftplib.FTP(args.host, args.user, args.password)
ftp.set_debuglevel(args.verbosity)

# change to document root on server
for dir in args.docroot.lstrip('/').split('/'):
	try:
		ftp.cwd(dir)
	except ftplib.error_perm:
		ftp.mkd(dir)
		ftp.cwd(dir)

################################################################################

# get offset from server time to local time
# also store timestamp file to allow PHP to get date/time of last update
file = open('.timestamp', 'w+b')
try:
	try:
		ftp.storbinary('STOR .timestamp', file)
	finally:
		file.close()

	local_time = os.path.getmtime('.timestamp')
	remote_time = ftp.sendcmd('MDTM .timestamp').split(None, 1)[1]
	remote_time = calendar.timegm(time.strptime(remote_time, '%Y%m%d%H%M%S'))
	time_offset = local_time - remote_time

finally:
	os.remove('.timestamp')

################################################################################

# check server features
features = ftp.sendcmd('FEAT')

# check for MLST support
use_mlst_modify_size = False
try:
	mlst_features = re.search('^(?<=\s+MLST\s+)\S*$', features, re.MULTILINE)
	if mlst_features:
		mlst_features = mlst_features.group(0).lowercase()
		if (re.search('(^|;)modify\*?;', mlst_features) and
			re.search('(^|;)size\*?;',   mlst_features)):

			ftp.sendcmd('OPTS MLST modify;size;')
			use_mlst_modify_size = True
			print 'using MLST'
except:
	pass

# check for TVFS support
use_tvfs = False

################################################################################

if args.clean:
	# build list of remote files, which is used to check for orphaned files
	def rlst(path=""):
		sys.stdout.write('.')
		sys.stdout.flush()
		results = []
		for file in ftp.nlst():

			# ignore files matching any of the "keep" regular expressions
			filePath = file
			if path:
				filePath = path + '/' + filePath
			for keep in args.keeps:
				if (re.match(keep, filePath) or os.altsep and
					re.match(keep, filePath.replace(os.sep, os.altsep))):
					print
					print 'keeping %s' % filePath
					continue

			# add file to results
			try:
				ftp.cwd(file)
				results += [file + '/' + next for next in rlst(filePath)]
				ftp.cwd('..')
			except:
				results.append(file)
		return results

	sys.stdout.write('indexing server')
	orphaned_files = rlst()
	print

# build tree of local directories and files to mirror
tree = {}
if args.files is not None:
	for file in args.files:
		tree.setdefault(os.path.dirname(file), []).append(os.path.basename(file))
else:
	# walk local filesystem to build tree
	for root, dirs, files in os.walk(LOCAL_ROOT):
		assert root.startswith(LOCAL_ROOT)
		root = root[len(LOCAL_ROOT) + 1:] or ''

		# exclude files and directories
		for exclude in args.excludes:
			for i in reversed(range(len(dirs))):
				dir = os.path.join(root, dirs[i])
				if (re.match(exclude, dir) or os.altsep and
					re.match(exclude, dir.replace(os.sep, os.altsep))):
					print 'excluding %s' % dir
					del dirs[i]
			for i in reversed(range(len(files))):
				file = os.path.join(root, files[i])
				if (re.match(exclude, file) or os.altsep and
					re.match(exclude, file.replace(os.sep, os.altsep))):
					print 'excluding %s' % file
					del files[i]

		if files:
			tree[root] = files

tree = sorted(tree.items(), key=lambda item: item[0])

# walk tree and upload changed files
lastDirs = []
for path, files in tree:

	# change current directory to path on server
	if use_tvfs:
		# FIXME: implement
		pass
	else:
		dirs = path.split(os.sep) if path else []

		# drop back to last common directory from last path, on server
		# this is an optimization; otherwise we would drop back to the docroot
		for i, (a, b) in enumerate(zip(lastDirs, dirs)):
			if a != b:
				for j in range(len(lastDirs) - i):
					ftp.cwd('..')
				break
		else:
			i = len(lastDirs)
		lastDirs = dirs
		dirs = dirs[i:]

		# drill down to directory on server
		for dir in dirs:
			try:
				ftp.cwd(dir)
			except ftplib.error_perm:
				ftp.mkd(dir)
				ftp.cwd(dir)

	# iterate files in directory
	for file in files:
		file_path = os.path.join(path, file)

		if args.clean:
			# remove from list of orphaned files
			try:
				orphaned_files.remove(file_path.replace(os.sep, '/'))
			except ValueError:
				pass

		# get local modification time and size
		local_time = os.path.getmtime(file_path)
		local_size = os.path.getsize(file_path)

		# get remote modification time and size
		# FIXME: this assumes the server supports SIZE (which is pretty likely)
		try:
			if use_mlst_modify_size:
				mlst = ftp.sendcmd('MLST ' + file).lowercase()
				remote_time = re.search('(?<=[\S;]modify=)[0-9]+(?=[\Z;])', mlst).group(0)
				remote_size = re.search('(?<=[\S;]size=)[0-9]+(?=[\Z;])',   mlst).group(0)
			else:
				remote_time = ftp.sendcmd('MDTM ' + file).split(None, 1)[1]
				remote_size = ftp.sendcmd('SIZE ' + file).split(None, 1)[1]
		except ftplib.error_perm:
			remote_time = remote_size = 0
		else:
			remote_time = calendar.timegm(time.strptime(remote_time, '%Y%m%d%H%M%S'))
			remote_time += time_offset
			remote_size = int(remote_size)

		if local_time > remote_time or local_size != remote_size:
			sys.stdout.write('uploading ' + file_path)
			if local_size > BLOCK_SIZE:
				def callback(block):
					sys.stdout.write('.')
					sys.stdout.flush()
			else:
				callback = None
			file_handle = open(file_path, 'rb')
			ftp.storbinary('STOR ' + file, file_handle, BLOCK_SIZE, callback)
			file_handle.close()
			print

if args.clean:
	# delete orphaned files on server
	# FIXME: this assumes the server supports TVFS
	ftp.cwd(args.docroot)
	for file in orphaned_files:
		sys.stdout.write('deleting ' + file)
		ftp.delete(file)
		print

ftp.quit()
