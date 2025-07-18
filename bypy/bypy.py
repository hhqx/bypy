#!/usr/bin/env python -tt
# encoding: utf-8
# PYTHON_ARGCOMPLETE_OK
# ===  IMPORTANT  ====
# NOTE: In order to support non-ASCII file names,
#       your system's locale MUST be set to 'utf-8'
# CAVEAT: DOESN'T seem to work with proxy, the underlying reason being
#         the 'requests' package used for http communication doesn't seem
#         to work properly with proxies, reason unclear.
# NOTE: It seems Baidu doesn't handle MD5 quite right after combining files,
#       so it may return erroneous MD5s. Perform a rapidupload again may fix the problem.
#        That's why I changed default behavior to no-verification.
# NOTE: syncup / upload, syncdown / downdir are partially duplicates
#       the difference: syncup/down compare and perform actions
#       while down/up just proceed to download / upload (but still compare during actions)
#       so roughly the same, except that sync can delete extra files
#
# TODO: Dry run?
# TODO: Use batch functions for better performance
# == NOTE ==
# Proxy is supported by the underlying Requests library,
# you can activate HTTP proxies by setting the HTTP_PROXY and / or
# HTTPS_PROXY environment variables respectively as follows:
#   HTTP_PROXY=http://user:password@domain
#   HTTPS_PROXY=http://user:password@domain
# (More information: http://docs.python-requests.org/en/master/user/advanced/#proxies)
# Though from my experience, it seems that some proxy servers may not be supported properly.

# from __future__ imports must occur at the beginning of the file
from __future__ import unicode_literals
from __future__ import print_function
from __future__ import division

import os
import sys
import io
import signal
import time
import shutil
import tempfile
import posixpath
import json
import hashlib
#import base64
import re
import pprint
import socket
import subprocess
import shlex
import atexit
from collections import deque
#from collections import OrderedDict
from functools import partial
from argparse import ArgumentParser
from argparse import RawDescriptionHelpFormatter
from copy import deepcopy

# unify Python 2 and 3
if sys.version_info[0] == 2:
	import urllib as ulp
	import cPickle as pickle
	pickleload = pickle.load
elif sys.version_info[0] == 3:
	import urllib.parse as ulp
	import pickle
	unicode = str
	basestring = str
	long = int
	raw_input = input
	pickleload = partial(pickle.load, encoding="bytes")

from . import const
from . import gvar
from . import printer_console
from . import cached as cachedm
from . import util
from . import printer
from .cached import (cached, stringifypickle, md5, crc32, slice_md5)
from .struct import PathDictTree
from .util import (
	iswindows,
	nop,
	perr, pwarn, pinfo, pdbg,
	jsondump, jsonload, formatex, rb,
	joinpath, get_pcs_path, print_pcs_list, str2bool, str2int,
	human_size, interpret_size, ls_time, ls_type,
	makedir, removedir, movefile, removefile, getfilesize,
	comp_semver,
	MyPrettyPrinter)
from .chkreq import (check_requirements, CheckResult)
from .requester import RequestsRequester

pr = printer_console.pr
prcolor = printer_console.prcolor
ask = printer_console.ask
pprgr = printer_console.pprgr

# there was a WantWriteError uncaught exception for Urllib3:
# https://github.com/shazow/urllib3/pull/412
# it was fixed here:
# https://github.com/shazow/urllib3/pull/413
# commit:
# https://github.com/shazow/urllib3/commit/a89dda000ed144efeb6be4e0b417c0465622fe3f
# and this was included in this commit in the Requests library
# https://github.com/kennethreitz/requests/commit/7aa6c62d6d917e11f81b166d1d6c9e60340783ac
# which was included in version 2.5.0 or above
# so minimum 2.5.0 is required
import requests
try:
	from requests.packages.urllib3.exceptions import ReadTimeoutError
except:
	try:
		from urllib3.exceptions import ReadTimeoutError
		ReadTimeoutError
	except:
		perr("Something seems wrong with the urllib3 installation.\nQuitting")
		sys.exit(const.EFatal)

from requests_toolbelt import multipart

# http://stackoverflow.com/a/27320254/404271
try:
	import multiprocess as mp
	from multiprocess import Pool
except ImportError:
	mp = None
	Pool = None
	perr("'multiprocess' library is not available, no parallel dl/ul.")

mpsemaphore = mp.Semaphore(1) if mp else None
cached.semaphore = mpsemaphore

if Pool != None:
	if sys.version_info[0] == 2:
		# enable with statement
		from contextlib import contextmanager
		@contextmanager
		def UPool(*args, **kwargs):
			yield Pool(*args, **kwargs)
	elif sys.version_info[0] == 3:
		UPool = Pool

pcsurl = const.PcsUrl
cpcsurl = const.CPcsUrl
dpcsurl = const.DPcsUrl

# This crap is here to avoid circular imports
# What a fantastic packing architecture Python has!
def makemppr(pr):
	def mppr(msg, *args, **kwargs):
		return pr(mp.current_process().name + ': ' + msg, *args, **kwargs)
	return mppr

def makemppprgr(pprgr):
	def mppprgr(finish, total, start_time = None, existing = 0,
		prefix = '', suffix = '', seg = 20):
		prefix = mp.current_process().name + ': ' + str(prefix)
		pprgr(finish, total, start_time, existing, prefix, suffix, seg)
	return mppprgr

def set_mp_print():
	global pr
	global prcolor
	global ask
	global pprgr
	opr = pr
	oprcolor = prcolor
	oask = ask
	opprgr = pprgr
	def restoremp():
		global pr
		global prcolor
		global ask
		global pprgr
		pr = cachedm.pr = util.pr = opr
		prcolor = util.prcolor = printer.prcolor = oprcolor
		ask = util.ask = oask
		pprgr = util.pprgr = opprgr

	pr = cachedm.pr = util.pr = makemppr(opr)
	prcolor = util.prcolor = printer.prcolor = makemppr(oprcolor)
	ask = util.ask = makemppr(oask)
	pprgr = util.pprgr = makemppprgr(opprgr)

	return restoremp

class ByPy(object):
	'''The main class of the bypy program'''
	# TODO: Apply to configdir instead of ~/.bypy
	@staticmethod
	def migratesettings():
		result = const.ENoError

		if os.path.exists(const.OldByPyCertsPath):
			removefile(const.OldByPyCertsPath)

		filesToMove = [
			[const.OldTokenFilePath, const.TokenFilePath],
			[const.OldPicklePath, const.PicklePath]
		]

		result = makedir(const.ConfigDir, 0o700) and result # make it secretive
		# this directory must exist
		if result != const.ENoError:
			perr("Fail to create config directory '{}'".format(const.ConfigDir))
			return result

		for tomove in filesToMove:
			oldfile = tomove[0]
			newfile = tomove[1]
			if os.path.exists(oldfile):
				dst = newfile
				if os.path.exists(newfile):
					dst = dst + '.old'
				result = movefile(oldfile, dst) and result

		# we move to JSON for hash caching for better portability
		# http://www.benfrederickson.com/dont-pickle-your-data/
		# https://kovshenin.com/2010/pickle-vs-json-which-is-faster/
		# JSON even outpeforms Pickle and definitely much more portable
		# DON'T bother with pickle.
		if os.path.exists(const.PicklePath):
			oldcache = {}
			try:
				with io.open(const.PicklePath, 'rb') as f:
					oldcache = pickleload(f)
				stringifypickle(oldcache)
				cached.loadcache(oldcache)
				cached.savecache(force_saving=True)
				pinfo("Contents of Pickle (old format hash cache) '{}' "
				"has been merged to '{}'".format(const.PicklePath, const.HashCachePath))
				mergedfile = const.PicklePath + '.merged'
				ok = movefile(const.PicklePath, mergedfile)
				if ok == const.ENoError:
					pinfo("Pickle (old format hash cache) '{}' "
					"has been renamed to '{}".format(const.PicklePath, mergedfile))
				else:
					perr("Failed to move Pickle (old format hash cache) '{}' to '{}'".format(const.PicklePath, mergedfile))
			except (
				pickle.PickleError,
				# the following is for dealing with corrupted cache file
				EOFError, TypeError, ValueError):
				invalidfile = const.PicklePath + '.invalid'
				ok = movefile(const.PicklePath, invalidfile)
				perr("{} invalid Pickle (old format hash cache) file '{}' to '{}'".format(
					"Moved" if ok == const.ENoError else "Failed to move",
					const.PicklePath, invalidfile))

		return result

	def setpcsurl(self, mirror):
		global pcsurl
		global cpcsurl
		global dpcsurl
		mirror = 'https://' + mirror + const.RestApiPath
		pcsurl = mirror
		cpcsurl = mirror
		dpcsurl = mirror
		# TODO: the SSL error may have gone, and the following is not needed
#		if mirror != const.PcsDomain:
#			# using a mirror, which has name mismatch SSL error, so need to disable SSL check
#			pwarn("Mirror '{}' used instead of the default PCS server url '{}', ".format(pcsurl, const.PcsUrl) +  \
#				  "we have to disable the SSL cert check in this case.")
#			self._checkssl = False

	def savesetting(self):
		try:
			jsondump(self._setting, self._settingpath, mpsemaphore)
		except Exception as ex:
			perr("Failed to save settings.\n{}".format(formatex(ex)))
			# complaining is enough, no need more actions as this is non-critical

	# def quit(self, retcode = const.ENoError):
	def cleanup(self):
		# saving is the most important
		# we save, but don't clean, why?
		# think about unmount path, moved files,
		# once we discard the information, they are gone.
		# so unless the user specifically request a clean,
		# we don't act too smart.
		#cached.cleancache()
		cached.savecache()
		self.savesetting()
		# if we flush() on Ctrl-C, we get
		# IOError: [Errno 32] Broken pipe
		sys.stdout.flush()
		# sys.exit(retcode)

	# TODO: this constructor is getting fat ...
	def __init__(self,
		slice_size = const.DefaultSliceSize,
		dl_chunk_size = const.DefaultDlChunkSize,
		verify = True,
		retry = 5, timeout = const.DefaultTimeOutInSeconds,
		quit_when_fail = False,
		resumedownload = True,
		extraupdate = lambda: (),
		incregex = '',
		ondup = '',
		followlink = True,
		checkssl = True,
		cacerts = None,
		rapiduploadonly = False,
		mirror = '',
		selectmirror = False,
		resumedl_revertcount = const.DefaultResumeDlRevertCount,
		deletesource = False,
		verbose = 0, debug = False,
		configdir = const.ConfigDir,
		requester = RequestsRequester,
		apikey = const.ApiKey,
		downloader = "",
		downloader_args = "",
		processes = const.DefaultProcessCount,
		secretkey = const.SecretKey):
		super(ByPy, self).__init__()
		self.jsonq = deque(maxlen = 64)

		# these two variables are without leading double underscore "__" as to export the as public,
		# so if any code using this class can check the current verbose / debug level
		cached.verbose = self.verbose = verbose
		cached.debug = self.debug = debug

		self._load_auth_server_list()

		if not cached.usecache:
			pinfo("Forced hash recalculation, hash cache won't be used")

		# handle backward compatibility, a.k.a. history debt
		sr = ByPy.migratesettings()
		if sr != const.ENoError:
			# bail out
			perr("Failed to migrate old settings.")
			sys.exit(const.EMigrationFailed)

		self._configdir = configdir.rstrip("/\\ ")
		makedir(self._configdir)
		# os.path.join() may not handle unicode well on Python 2.7
		self._progresspath = configdir + os.sep + const.ProgressFileName
		self._tokenpath = configdir + os.sep + const.TokenFileName
		self._settingpath = configdir + os.sep + const.SettingFileName
		self._setting = {}

		self._downloader = downloader.lower().strip()
		if downloader_args:
			self._downloader_args = downloader_args
		else:
			if downloader in const.DownloaderDefaultArgs:
				self._downloader_args = const.DownloaderDefaultArgs[downloader]
			else:
				self._downloader_args = ''

		if os.path.exists(self._settingpath):
			try:
				self._setting = jsonload(self._settingpath)
			except Exception as ex:
				perr("Error loading settings, using default settings.{}".format(formatex(ex)))
		self._hashcachepath = configdir + os.sep + const.HashCacheFileName
		cached.hashcachepath = self._hashcachepath
		self._certspath = os.path.join(os.path.dirname(__file__), const.ByPyCertsFileName)

		self._requester = requester
		self._apikey = apikey
		self._secretkey = secretkey
		self._use_server_auth = not secretkey

		self._slice_size = slice_size
		self._dl_chunk_size = dl_chunk_size
		self._verify = verify
		self._retry = retry
		self._quit_when_fail = quit_when_fail
		self._timeout = timeout
		self._resumedownload = resumedownload
		self._extraupdate = extraupdate
		self._incregex = incregex
		self._incregmo = re.compile(incregex)
		if ondup and len(ondup) > 0:
			self._ondup = ondup[0].upper()
		else:
			self._ondup = 'O' # O - Overwrite* S - Skip P - Prompt
		self._followlink = followlink
		self._rapiduploadonly = rapiduploadonly
		self._resumedl_revertcount = resumedl_revertcount
		self._deletesource = deletesource
		if deletesource:
			self.pd("Forcing verification since we will delete source for successful transfers.")
			self._verify = True
		self.processes = processes

		# in case of abortions, exceptions, etc
		atexit.register(self.cleanup)

		#TODO: SSL verification causes some much trouble for different Python version
		# I give up and disable it for good, or for bad
		checkssl = False
		# set SSL Check flag before mirror configuration, which may change it
		self._checkssl = checkssl
		# --mirror takes precedence over --select-fastest-mirror
		mirror = mirror.lower()
		if len(mirror) > 0 and mirror != const.PcsDomain:
			self.setpcsurl(mirror)
		elif selectmirror:
			self._select_fastest_mirror()
		# else: already set in global

		if self._checkssl:
			# sort of undocumented by requests
			# http://stackoverflow.com/questions/10667960/python-requests-throwing-up-sslerror
			if cacerts is not None:
				if os.path.isfile(cacerts):
					self._certspath = cacerts
				else:
					perr("Invalid CA Bundle '{}' specified")

			# falling through here means no customized CA Certs specified
			if self._checkssl is True:
				# use our own CA Bundle if possible
				if os.path.isfile(self._certspath):
					self._checkssl = self._certspath
				else:
					# Well, disable cert verification
					pwarn(
"** SSL Certificate Verification has been disabled **\n\n"
"If you are confident that your CA Bundle can verify "
"Baidu PCS's certs, you can run the prog with the '" + const.CaCertsOption + \
" <your ca cert path>' argument to enable SSL cert verification.\n\n"
"However, most of the time, you can ignore this warning, "
"you are not going to send sensitive data to the cloud plainly right?")
					self._checkssl = False
		if not checkssl:
			requester.disable_warnings(self.debug)

		cached.loadcache()
		requester.set_logging_level(debug)
		# useful info for debugging
		if debug > 0:
			pr("----")
			pr("Verbose level = {}".format(verbose))
			pr("Debug level = {}".format(debug))
			# these informations are useful for debugging
			pr("Config directory: '{}'".format(self._configdir))
			pr("Token file: '{}'".format(self._tokenpath))
			pr("Hash Cache file: '{}'".format(self._hashcachepath))
			pr("App root path at Baidu Yun '{}'".format(const.AppPcsPath))
			pr("sys.stdin.encoding = {}".format(sys.stdin.encoding))
			pr("sys.stdout.encoding = {}".format(sys.stdout.encoding))
			pr("sys.stderr.encoding = {}".format(sys.stderr.encoding))
			pr("----\n")

		# the prophet said: thou shalt initialize
		self._existing_size = 0
		self._json = {}
		self._access_token = ''
		self._remote_json = {}
		self._slice_md5s = []
		self._cookies = {}
		# TODO: whether this works is still to be tried out
		self._isrev = False
		self._rapiduploaded = False

		# store the response object, mainly for testing.
		self.response = object()
		# store function-specific result data
		self.result = {}

		if not self._load_local_json():
			# no need to call _load_local_json() again as _auth() will load the json & acess token.
			result = self._auth()
			if result != const.ENoError:
				perr("Program authorization FAILED.\n"
					"You need to authorize this program before using any PCS functions.\n"
					"Quitting...\n")
				sys.exit(result)

		for proxy in ['HTTP_PROXY', 'HTTPS_PROXY']:
			if proxy in os.environ:
				pr("{} used: {}".format(proxy, os.environ[proxy]))

		# update check
		check_update = False
		nowsec = int(time.time())
		if const.SettingKey_LastUpdateCheckTime not in self._setting:
			check_update = True
		else:
			lastcheck = self._setting[const.SettingKey_LastUpdateCheckTime]
			if nowsec - lastcheck > 7 * 24 * 60 * 60: # check every 7 days
				check_update = True

		if check_update:
			try:
				r = requests.get('https://raw.githubusercontent.com/houtianze/bypy/master/update/update.json')
				if r.status_code == 200:
					try:
						j = r.json()
						min_ver_key = 'minimumRequiredVersion'
						if min_ver_key in j:
							minver = j[min_ver_key]
							if comp_semver(const.__version__, minver) < 0:
								perr("Your current version ({}) is too low, "
									"minimum required version is {}.\n"
									"Please run 'pip install -U bypy' to update and try again.".format(
										const.__version__, minver))
								sys.exit(const.EUpdateNeeded)
							else:
								self._setting[const.SettingKey_LastUpdateCheckTime] = nowsec
								self.savesetting()

						recommended_ver_key = 'recommendedVersion'
						if recommended_ver_key in j:
							recver = j[recommended_ver_key]
							if comp_semver(const.__version__, recver) < 0:
								pr("Your current version ({}) is low, "
									"It's recommended to update to version {}.\n"
									"Please run 'pip install -U bypy' to update.".format(
										const.__version__, recver))
					except ValueError:
						self.pd("Invalid response for update check, skipping.")
				else:
					self.pd("HTTP Status {} while checking update, skipping.".format(r.status_code))
			except:
				self.pd("Error occurred while checking update, skipping.")

	def pv(self, msg, **kwargs):
		if self.verbose:
			pr(msg)

	def pd(self, msg, level = 1, **kwargs):
		if self.debug and self.debug >= level:
			pdbg(msg, kwargs)

	def shalloverwrite(self, prompt):
		if self._ondup == 'S':
			return False
		elif self._ondup == 'P':
			ans = ask(prompt, False).upper()
			if not ans.startswith('Y'):
				return False

		return True

	def _warn_multi_processes(self, process = "dl / ul"):
		if process:
			pwarn("Running multiple ({}) processes for {}, I don't know if things will go awry...".format(
				self.processes, process))

	def _check_prompt_multiprocess(self):
		if self._ondup == 'P' and Pool and self.processes != 1:
			errmsg = "Can't do parallel processing if you want to prompt the user for overwrite confirmation"
			perr(errmsg)
			return False
		return True

	def _filter_multi_results(self, results):
		# http://www.diveintopython3.net/porting-code-to-python-3-with-2to3.html#filter
		errors = list(filter(lambda x: x != const.ENoError, results))
		return const.ENoError if len(errors) == 0 else errors[-1]

	def _multi_process(self, worker, iterator, process = "dl / ul"):
		if not self._check_prompt_multiprocess():
			return const.EArgument
		self._warn_multi_processes(process)
		# patch the printer
		restoremp = set_mp_print()
		with UPool(self.processes) as pool:
			try:
				# http://stackoverflow.com/a/35134329/404271
				# On Python 2.x, Ctrl-C won't work if we use pool.map(), wait() or get()
				ar = pool.map_async(worker, iterator)
				#results = ar.get(const.TenYearInSeconds)
				results = ar.get(const.FortyNineDaysInSeconds)
				restoremp()
				return self._filter_multi_results(results)
			except pickle.PicklingError as pe:
				errmsg = ("Exception:\n{}\n"
						"--------------------------------\n"
						"Error: Your Python 'multiprocess' library is probably "
						"not properly installed (missing C extensions). "
						"You need to install a C compiler and Python headers before "
						"installing the Python 'multiprocess' library. "
						"(All these hassles could have been saved if Python's builtin "
						"'multiprocessing' works properly, sigh.)\n"
						"Fix for debian derivatives:\n"
						"- Install gcc: # apt-get install gcc\n"
						"- Install python-dev: # apt-get install python-dev\n"
						"- Reinstall Python 'multiprocess' library:\n"
						"  # pip uninstall -y multiprocess\n"
						"  # pip install -v multiprocess\n"
						"- If there's no errors/warnings in the above actions, "
						"then this error should be gone when you run 'bypy' with '{}' again.\n"
						).format(formatex(pe), const.MultiprocessOption)
				perr(errmsg)
				sys.exit(const.EFatal)

	def _print_error_json(self, r):
		try:
			dj = r.json()
			if 'error_code' in dj and 'error_msg' in dj:
				ec = dj['error_code']
				et = dj['error_msg']
				msg = ''
				if ec == const.IEMD5NotFound:
					pf = pinfo
					msg = et
				else:
					pf = perr
					msg = "Error JSON returned:{}\nError code: {}\nError Description: {}".format(dj, ec, et)
				pf(msg)
		except Exception as ex:
			perr('Error parsing JSON Error Code from:\n{}\n{}'.format(rb(r.text), formatex(ex)))

	def _dump_exception(self, ex, url, pars, r, act):
		if self.debug or self.verbose:
			perr("Error accessing '{}'".format(url))
			if self.debug:
				perr(formatex(ex))
			perr("Function: {}".format(act.__name__))
			perr("Website parameters: {}".format(pars))
			if r != None:
				# just playing it safe
				if hasattr(r, 'url'):
					perr("Full URL: {}".format(r.url))
				if hasattr(r, 'status_code') and hasattr(r, 'text'):
					perr("HTTP Response Status Code: {}".format(r.status_code))
					if (r.status_code != 200 and r.status_code != 206) \
						or (not ('method' in pars and pars['method'] == 'download') \
							and url.find('method=download') == -1 \
							and url.find('baidupcs.com/file/') == -1):
						self._print_error_json(r)
						perr("Website returned: {}".format(rb(r.text)))

	# child class override this to to customize error handling
	def _handle_more_response_error(self, r, sc, ec, act, actargs):
		return const.ERequestFailed

	# Workaround for Baidu returning wrong HTTP status code
	# Who in one's right mind would send error with an HTTP 200 status?
	# <D> HTTP Status Code: 200
	# <D> 200 OK, processing action
	# <D> Response: {'error_code': 111, 'error_msg': 'Access token expired'}
	def _is_error_response(self, r):
		# don't attempt on large contents
		isJson = False
		ContentTypeKey = 'Content-Type'
		contentType = r.headers.get(ContentTypeKey)
		if (contentType and 'json' in contentType):
			isJson = True

		if len(r.content) > 10 * 1024 or not isJson:
			return False
		try:
			j = r.json()
			if 'error_code' in j and j['error_code'] != 0:
				return True
		except ValueError:
			return False
		return False

	def _get_json_errorcode(self, r, act, defaultec = const.ERequestFailed):
		try:
			j = r.json()
			self.pd("Website returned JSON: {}".format(j))
			if 'error_code' in j:
				return j['error_code']
			else:
				return defaultec
		except ValueError:
			if hasattr(r, 'text'):
				self.pd("Website Response: {}".format(rb(r.text)))
			if act == self._cdl_act:
				return const.IETaskNotFound
			return defaultec

	def _request_work_die(self, ex, url, pars, r, act):
		result = const.EFatal
		self._dump_exception(ex, url, pars, r, act)
		perr("Fatal Exception, no way to continue.\nQuitting...\n")
		perr("If the error is reproducible, run the program with `-dv` arguments again to get more info.\n")
		sys.exit(result)
		# we eat the exception, and use return code as the only
		# error notification method, we don't want to mix them two
		#raise # must notify the caller about the failure

	def _request_work(self, url, pars, act, method, actargs = None, addtoken = True, dumpex = True, **kwargs):
		result = const.ENoError
		r = None

		self._extraupdate()
		parsnew = pars.copy()
		if addtoken:
			parsnew['access_token'] = self._access_token

		try:
			self.pd(method + ' ' + url)
			self.pd("actargs: {}".format(actargs))
			self.pd("Params: {}".format(pars))

			r = self._requester.request(method, url, params = parsnew, timeout = self._timeout, verify = self._checkssl, **kwargs)
			self.response = r
			sc = r.status_code
			self.pd("Full URL: {}".format(r.url))
			self.pd("HTTP Status Code: {}".format(sc))
			# BUGFIX: DON'T do this, if we are downloading a big file,
			# the program will eat A LOT of memeory and potentially hang / get killed
			#self.pd("Request Headers: {}".format(pprint.pformat(r.request.headers)), 2)
			#self.pd("Response Header: {}".format(pprint.pformat(r.headers)), 2)
			#self.pd("Response: {}".format(rb(r.text)), 3)
			# 206 Partial Content
			if (sc == requests.codes.ok or sc == 206) and not self._is_error_response(r):
				if sc == requests.codes.ok:
					# #162 https://github.com/houtianze/bypy/pull/162
					# handle response like this:  {"error_code":0,"error_msg":"no error","request_id":70768340515255385}
					if not ('method' in pars and pars['method'] == 'download'):
						try:
							j = r.json()
							if 'error_code' in j and j['error_code'] == 0 and 'error_msg' in j and j['error_msg'] == 'no error':
								self.pd("Unexpected response: {}".format(j))
								return const.ERequestFailed
						except Exception as ex:
							perr(formatex(ex))
							# TODO: Shall i return this?
							return const.ERequestFailed

					self.pd("200 OK, processing action")
				else:
					self.pd("206 Partial Content (this is OK), processing action")
				result = act(r, actargs)
				if result == const.ENoError:
					self.pd("Request OK")
			else:
				ec = self._get_json_errorcode(r, act)
				#   6 (sc: 403): No permission to access user data
				# 110 (sc: 401): Access token invalid or no longer valid
				# 111 (sc: 401): Access token expired
				if ec == 111 or ec == 110 or ec == 6 or ec == const.IEAppIDIsEmpty: # and sc == 401:
					self.pd("ec = {}".format(ec))
					self.pd("Need to refresh token, refreshing")
					if const.ENoError == self._refresh_token(): # refresh the token and re-request
						# TODO: avoid infinite recursive loops
						# TODO: properly pass retry
						result = self._request(url, pars, act, method, actargs, True, addtoken, dumpex, **kwargs)
					else:
						result = const.EFatal
						perr("FATAL: Token refreshing failed, can't continue.\nQuitting...\n")
						sys.exit(result)
				# File md5 not found, you should use upload API to upload the whole file.
				elif ec == const.IEMD5NotFound: # and sc == 404:
					self.pd("MD5 not found, rapidupload failed")
					result = ec
				# superfile create failed
				elif ec == const.IESuperfileCreationFailed \
					or ec == const.IEBlockMissInSuperFile2:
					self.pd("Failed to combine files from MD5 slices")
					result = ec
				elif ec == const.IEFileAlreadyExists: # ec == 31061, sc == 400, file already exists
					# explanations:
					# this error occurs when we try to create a directory at Baidu PCS when it already exists,
					# this almost only happens when we upload using multi-process (double/multiple-directory creation).
					# this error is innocuous, we can safely ignore it
					# (actually, we don't need to do anything further and should just continue as usual).
					# so to avoid confusions to the user and potential logic errors (more importantly) in the future,
					# we just "lie" and force it to "no error".
					# though this is not 100% accurate technically, i feel it's accurate enough for the users
					self.pd("Faking error_code {} to {}, this is safe to ignore.".format(
						const.IEFileAlreadyExists, const.ENoError))
					result = const.ENoError
				# errors that make retrying meaningless
				elif (
					ec == 31062 or # sc == 400 file name is invalid
					ec == 31063 or # sc == 400 file parent path does not exist
					ec == 31064 or # sc == 403 file is not authorized
					ec == 31065 or # sc == 400 directory is full
					ec == 31066 or # sc == 403 (indeed 404) file does not exist
					ec == const.IETaskNotFound or # 36016 or # sc == 404 Task was not found
					ec == const.IEParameterError or # sc == 400 param error
					# the following was found by xslidian, but i have never ecountered before
					ec == 31390):  # sc == 404 # {"error_code":31390,"error_msg":"Illegal File"} # r.url.find('http://bcscdn.baidu.com/bcs-cdn/wenxintishi') == 0
					result = ec
					# TODO: Move this out to cdl_cancel() ?
					#if ec == const.IETaskNotFound:
					#	pr(r.json())
					if dumpex:
						self._dump_exception(None, url, pars, r, act)
				else:
					# gate for child classes to customize behaviors
					# the function should return ERequestFailed if it doesn't handle the case
					result = self._handle_more_response_error(r, sc, ec, act, actargs)
					if result == const.ERequestFailed and dumpex:
						self._dump_exception(None, url, pars, r, act)
		except (requests.exceptions.RequestException,
				socket.error,
				ReadTimeoutError) as ex:
			# If certificate check failed, no need to continue
			# but prompt the user for work-around and quit
			# why so kludge? because requests' SSLError doesn't set
			# the errno and strerror due to using **kwargs,
			# so we are forced to use string matching
			if isinstance(ex, requests.exceptions.SSLError) \
				and re.match(r'^\[Errno 1\].*error:14090086.*:certificate verify failed$', str(ex), re.I):
				# [Errno 1] _ssl.c:504: error:14090086:SSL routines:SSL3_GET_SERVER_CERTIFICATE:certificate verify failed
				result = const.EFatal
				self._dump_exception(ex, url, pars, r, act)
				perr("\n\n== Baidu's Certificate Verification Failure ==\n"
				"We couldn't verify Baidu's SSL Certificate.\n"
				"It's most likely that the system doesn't have "
				"the corresponding CA certificate installed.\n"
				"There are two ways of solving this:\n"
				"Either) Run this prog with the '" + const.CaCertsOption + \
				" <path to " + const.ByPyCertsFileName + "> argument "
				"(" + const.ByPyCertsFileName + " comes along with this prog). "
				"This is the secure way. "
				"However, it won't work after 2020-02-08 when "
				"the certificat expires.\n"
				"Or) Run this prog with the '" + const.DisableSslCheckOption + \
				"' argument. This supresses the CA cert check "
				"and always works.\n")
				# sys.exit(result)
				raise

			# why so kludge? because requests' SSLError doesn't set
			# the errno and strerror due to using **kwargs,
			# so we are forced to use string matching
			if isinstance(ex, requests.exceptions.SSLError) \
				and re.match(r'^\[Errno 1\].*error:14090086.*:certificate verify failed$', str(ex), re.I):
				# [Errno 1] _ssl.c:504: error:14090086:SSL routines:SSL3_GET_SERVER_CERTIFICATE:certificate verify failed
				perr("\n*** We probably don't have Baidu's CA Certificate ***\n" \
				"This in fact doesn't matter most of the time.\n\n" \
				"However, if you are _really_ concern about it, you can:\n" \
				"Either) Run this prog with the '" + const.CaCertsOption + \
				" <path to bypy.cacerts.pem>' " \
				"argument. This is the secure way.\n" \
				"Or) Run this prog with the '" + const.DisableSslCheckOption + \
				"' argument. This suppresses the CA cert check.\n")

			result = const.ERequestFailed
			if dumpex:
				self._dump_exception(ex, url, pars, r, act)

		# TODO: put this check into the specific funcitons?
		except ValueError as ex:
			if ex.message == 'No JSON object could be decoded':
				result = const.ERequestFailed
				if dumpex:
					self._dump_exception(ex, url, pars, r, act)
			else:
				result = const.EFatal
				# self._request_work_die(ex, url, pars, r, act)
				raise

		except Exception as ex:
			# OpenSSL SysCallError
			if ex.args == (10054, 'WSAECONNRESET') \
			or ex.args == (10053, 'WSAECONNABORTED') \
			or ex.args == (104, 'ECONNRESET') \
			or ex.args == (110, 'ETIMEDOUT') \
			or ex.args == (32, 'EPIPE'):
				result = const.ERequestFailed
				if dumpex:
					self._dump_exception(ex, url, pars, r, act)
			else:
				result = const.EFatal
				# self._request_work_die(ex, url, pars, r, act)
				raise

		return result

	def _request(self, url, pars, act, method, actargs = None, retry = True, addtoken = True, dumpex = True, **kwargs):
		tries = 1
		if retry:
			tries = self._retry

		result = const.ERequestFailed

		# Change the User-Agent to avoid server fuss
		kwnew = kwargs.copy()
		if 'headers' not in kwnew:
			kwnew['headers'] = { 'User-Agent': const.UserAgent }

		# Now, allow User-Agent to be set in the caller, instead of always using the default UserAgent value.
		if 'User-Agent' not in kwnew['headers']:
			kwnew['headers']['User-Agent'] = const.UserAgent

		i = 0
		while True:
			result = self._request_work(url, pars, act, method, actargs, addtoken, dumpex, **kwnew)
			i += 1
			# only ERequestFailed needs retry, other error still directly return
			if result == const.ERequestFailed:
				if i < tries:
					# algo changed: delay more after each failure
					delay = const.RetryDelayInSec * i
					perr("Waiting {} seconds before retrying...".format(delay))
					time.sleep(delay)
					perr("Request Try #{} / {}".format(i + 1, tries))
				else:
					result = const.EMaxRetry
					perr("Maximum number ({}) of tries failed.".format(tries))
					if self._quit_when_fail:
						sys.exit(const.EMaxRetry)
					break
			else:
				break

		return result

	def _get(self, url, pars, act, actargs = None, retry = True, addtoken = True, dumpex = True, **kwargs):
		return self._request(url, pars, act, 'GET', actargs, retry, addtoken, dumpex, **kwargs)

	def _post(self, url, pars, act, actargs = None, retry = True, addtoken = True, dumpex = True, **kwargs):
		return self._request(url, pars, act, 'POST', actargs, retry, addtoken, dumpex, **kwargs)

	# direction: True - upload, False - download
	def _shallinclude(self, lpath, rpath, direction):
		arrow = '==>' if direction else '<=='
		checkpath = lpath if direction else rpath
		# TODO: bad practice, see os.access() document for more info
		if direction: # upload
			if not os.path.exists(lpath):
				perr("'{}' {} '{}' skipped since local path no longer exists".format(
					lpath, arrow, rpath))
				return False
		else: # download
			if os.path.exists(lpath) and (not os.access(lpath, os.R_OK)):
				perr("'{}' {} '{}' skipped due to permission".format(
					lpath, arrow, rpath))
				return False

		if '\\' in os.path.basename(checkpath):
			perr("'{}' {} '{}' skipped due to problemic '\\' in the path".format(
				lpath, arrow, rpath))
			return False

		include = (not self._incregex) or self._incregmo.match(checkpath)
		if not include:
			self.pv("'{}' {} '{}' skipped as it's not included in the regex pattern".format(
				lpath, arrow, rpath))

		return include

	ListFormatDict = {
		'$t' : (lambda json: ls_type(json['isdir'])),
		'$f' : (lambda json: json['path'].split('/')[-1]),
		'$c' : (lambda json: ls_time(json['ctime'])),
		'$m' : (lambda json: ls_time(json['mtime'])),
		'$d' : (lambda json: str(json['md5'] if 'md5' in json else '')),
		'$s' : (lambda json: str(json['size'])),
		'$i' : (lambda json: str(json['fs_id'])),
		'$b' : (lambda json: str(json['block_list'] if 'block_list' in json else '')),
		'$u' : (lambda json: 'HasSubDir' if 'ifhassubdir' in json and json['ifhassubdir'] else 'NoSubDir'),
	}

	def _replace_list_format(self, fmt, j):
		output = fmt
		for k, v in ByPy.ListFormatDict.items():
			output = output.replace(k, v(j))
		self.file_list.append(output)
		return output

	def _load_local_json(self):
		try:
			self._json = jsonload(self._tokenpath)
			self._access_token = self._json['access_token']
			self.pd("Token loaded:")
			self.pd(self._json)
			return True
		except Exception as ex:
			self.pd("Error while loading baidu pcs token.\n{}".format(formatex(ex)))
			return False

	def _store_json_only(self, j):
		self._json = j
		self._access_token = self._json['access_token']
		self.pd("access token: " + self._access_token)
		self.pd("Authorize JSON:")
		self.pd(self._json)
		tokenmode = 0o600
		try:
			jsondump(self._json, self._tokenpath, mpsemaphore)
			os.chmod(self._tokenpath, tokenmode)
			return const.ENoError
		except Exception as ex:
			perr("Exception occured while trying to store access token:\n{}".format(
				formatex(ex)))
			return const.EFileWrite

	def _prompt_clean(self):
		pinfo('-' * 64)
		pinfo("""This is most likely caused by authorization errors.
Possible causes:
 - You didn't run this program for a long time (more than a month).
 - You changed your Baidu password after authorizing this program.
 - You didn't give this program the 'netdisk' access while authorizing.
 - ...
Possible fixes:
 1. Remove the authorization token by running with the parameter '{}', and then re-run this program.
 2. If (1) still doesn't solve the problem, you may have to go to:
    https://passport.baidu.com/accountbind
    and remove the authorization of this program, and then re-run this program.""".format(const.CleanOptionShort))
		return const.EInvalidJson

	def _store_json(self, r):
		j = {}
		try:
			j = r.json()
		except Exception as ex:
			perr("Failed to decode JSON:\n{}".format(formatex(ex)))
			perr("Error response:\n{}".format(r.text))
			return self._prompt_clean()

		if 'access_token' not in j:
			perr("Invalid token JSON:\n{}".format(j))
			return const.EInvalidJson
		return self._store_json_only(j)

	def _server_auth_act(self, r, args):
		return self._store_json(r)

	def _local_auth_act(self, r, args):
		return self._store_json(r)

	def _repr_timeout(self):
		return self._timeout if self._timeout else 'infinite'

	def _load_auth_server_list(self):
		# https://stackoverflow.com/a/58941536/404271
		import pkgutil
		j = json.loads(pkgutil.get_data(__name__, 'res/auth.json'))
		self.pd('Auth servers loaded: {}'.format(j))
		self.AuthServerList = j['AuthServerList']
		self.RefreshServerList = j['RefreshServerList']

	def _update_auth_server_list(self):
		try:
			r = requests.get('https://raw.githubusercontent.com/houtianze/bypy/master/bypy/res/auth.json')
			if r.status_code == 200:
				try:
					j = r.json()
					self.pd('Auth servers updated: {}'.format(j))
					self.AuthServerList = j['AuthServerList']
					self.RefreshServerList = j['RefreshServerList']
				except ValueError:
					self.pd("Invalid response for auth servers update, skipping.")
			else:
				self.pd("HTTP Status {} while updating auth servers, skipping.".format(r.status_code))
		except:
			self.pd("Error occurred while updating auth servers, skipping.")

	def _auth(self):
		params = {
			'client_id' : self._apikey,
			'response_type' : 'code',
			'redirect_uri' : 'oob',
			'scope' : 'basic netdisk' }
		pars = ulp.urlencode(params)
		msg = 'Please visit:\n{}\nAnd authorize this app'.format(const.ServerAuthUrl + '?' + pars) + \
			'\nPaste the Authorization Code here within 10 minutes.'
		auth_code = ''
		while True:
			auth_code = ask(msg).strip()
			if auth_code and len(auth_code) >= 16: # 16 is just random lower bound
				break
		self.pd("auth_code: {}".format(auth_code))
		pr('Authorizing, please be patient, it may take upto {} seconds...'.format(self._repr_timeout()))

		if self._use_server_auth:
			pars = {
				'code' : auth_code,
				'bypy_version' : const.__version__,
				'redirect_uri' : 'oob' }

			result = None
			# TODO: hacky
			global perr
			savedperr = perr
			if not self.debug:
				perr = nop

			self._update_auth_server_list()
			for auth in self.AuthServerList:
				(url, retry, msg) = auth
				pr(msg)
				result = self._get(url, pars, self._server_auth_act, retry = retry, addtoken = False)
				if result == const.ENoError:
					break
			if not self.debug:
				perr = savedperr

			if result == const.ENoError:
				pr("Successfully authorized")
			else:
				perr("Fatal: All server authorizations failed.")
				self._prompt_clean()
				sys.exit(result)
		else:
			pars = {
				'grant_type' : 'authorization_code',
				'code' : auth_code,
				'client_id' : self._apikey,
				'client_secret' : self._secretkey,
				'redirect_uri' : 'oob'
			}
			result = self._post(const.TokenUrl, pars, self._local_auth_act, addtoken = False)

		return result

	def _device_auth_act(self, r, args):
		dj = r.json()
		self.pd('device response: {}'.format(dj))
		return self._get_token(dj)

	def _device_auth(self):
		pars = {
			'client_id' : self._apikey,
			'response_type' : 'device_code',
			'scope' : 'basic netdisk'}
		return self._get(const.DeviceAuthUrl, pars, self._device_auth_act, addtoken = False)

	def _get_token_act(self, r, args):
		return self._store_json(r)

	def _get_token(self, deviceJson):
		# msg = "Please visit:{}\n" + deviceJson['verification_url'] + \
		# 	  "\nwithin " + str(deviceJson['expires_in']) + " seconds\n"
		# "Input the CODE: {}\n".format(deviceJson['user_code'])" + \
		# 	"and Authorize this little app.\n"
		msg = "Please visit:\n{}\nwithin {} seconds\n" \
			"Input the CODE: {}\n" \
			"and Authorize this little app.\n".format(
				deviceJson['verification_url'],
				str(deviceJson['expires_in']),
				deviceJson['user_code'])
		ask(msg)

		pars = {
			'grant_type' : 'device_token',
			'code' :  deviceJson['device_code'],
			'client_id' : self._apikey,
			'client_secret' : self._secretkey}

		return self._get(const.TokenUrl, pars, self._get_token_act, addtoken = False)

	def _refresh_token_act(self, r, args):
		return self._store_json(r)

	def _refresh_token(self):
		if self._use_server_auth:
			pr('Refreshing, please be patient, it may take upto {} seconds...'.format(self._repr_timeout()))

			pars = {
				'bypy_version' : const.__version__,
				'refresh_token' : self._json['refresh_token'] }

			result = None
			# TODO: hacky
			global perr
			savedperr = perr
			if not self.debug:
				perr = nop
			self._update_auth_server_list()
			for refresh in self.RefreshServerList:
				(url, retry, msg) = refresh
				pr(msg)
				result = self._get(url, pars, self._refresh_token_act, retry = retry, addtoken = False)
				if result == const.ENoError:
					break
			if not self.debug:
				perr = savedperr

			if result == const.ENoError:
				pr("Token successfully refreshed")
			else:
				perr("Token-refreshing on all the servers failed")
				self._prompt_clean()
				sys.exit(result)

			return result
		else:
			pars = {
				'grant_type' : 'refresh_token',
				'refresh_token' : self._json['refresh_token'],
				'client_secret' : self._secretkey,
				'client_id' : self._apikey}
			return self._post(const.TokenUrl, pars, self._refresh_token_act)

	def _walk_normal_file(self, dir):
		#dirb = dir.encode(FileSystemEncoding)
		for walk in os.walk(dir, followlinks=self._followlink):
			normalfiles = [t for t in walk[-1]
								if os.path.isfile(os.path.join(walk[0], t))]
			normalfiles.sort()
			normalwalk = walk[:-1] + (normalfiles,)
			yield normalwalk

	def _quota_act(self, r, args):
		j = r.json()
		self.jsonq.append(j)
		pr('Quota: ' + human_size(j['quota']))
		pr('Used: ' + human_size(j['used']))
		return const.ENoError

	@staticmethod
	def help(command): # this comes first to make it easy to spot
		''' Usage: help <command> - provide some information for the command '''
		for i, v in ByPy.__dict__.items():
			if callable(v) and v.__doc__ and v.__name__ == command :
				help = v.__doc__.strip()
				pos = help.find(const.HelpMarker)
				if pos != -1:
					pr("Usage: " + help[pos + len(const.HelpMarker):].strip())

	def refreshtoken(self):
		''' Usage: refreshtoken - refresh the access token '''
		return self._refresh_token()

	def _remove_remote_on_success(self, remotepath):
		if self._deletesource:
			self.pd("Removing remote path '{}' after successful download.".format(remotepath))
			result = self._delete(remotepath)
			if result == const.ENoError:
				self.pd("Remote path '{}' removed.".format(remotepath))
			else:
				perr("Failed to remove remote path '{}'.".format(remotepath))
			return result

	def _remove_local_on_success(self, localpath):
		if self._deletesource:
			self.pd("Removing local path '{}' after successful upload.".format(localpath))
			result = cached.remove_path_and_cache(localpath)
			if result == const.ENoError:
				self.pd("Local path '{}' removed.".format(localpath))
			else:
				perr("Failed to remove local path '{}'.".format(localpath))
			return result

	def info(self):
		return self.quota()

	def quota(self):
		''' Usage: quota/info - display the quota information '''
		pars = {
			'method' : 'info' }
		return self._get(pcsurl + 'quota', pars, self._quota_act)

	def _whoami_act(self, r, args):
		j = r.json()
		self.jsonq.append(j)
		self.pd("Response: {}".format(j))
		pr('User id: ' + j['userid'])
		pr('Username: ' + j['username'])
		return const.ENoError

	def whoami(self):
		''' Usage: whoami - display the user information '''
		pars = {}
		return self._get(const.OpenApiUrl + '/rest/2.0/passport/users/getInfo', pars, self._whoami_act)

	# return:
	#   0: local and remote files are of same size
	#   1: local file is larger
	#   2: remote file is larger
	#  -1: inconclusive (probably invalid remote json)
	def _compare_size(self, lsize, rjson):
		if 'size' in rjson:
			rsize = rjson['size']
			if lsize == rsize:
				return 0
			elif lsize > rsize:
				return 1
			else:
				return 2
		else:
			return -1

	def _verify_current_file(self, j, gotlmd5):
		# if we really don't want to verify
		if self._current_file == '/dev/null' and not self._verify:
			return const.ENoError

		rsize = 0
		rmd5 = 0

		# always perform size check even _verify is False
		if 'size' in j:
			rsize = j['size']
		else:
			perr("Unable to verify JSON: '{}', as no 'size' entry found".format(j))
			return const.EHashMismatch

		if 'md5' in j:
			rmd5 = j['md5']
		#elif 'block_list' in j and len(j['block_list']) > 0:
		#	rmd5 = j['block_list'][0]
		#else:
		#	# quick hack for meta's 'block_list' field
		#	pwarn("No 'md5' nor 'block_list' found in json:\n{}".format(j))
		#	pwarn("Assuming MD5s match, checking size ONLY.")
		#	rmd5 = self._current_file_md5
		else:
			perr("Unable to verify JSON: '{}', as no 'md5' entry found".format(j))
			return const.EHashMismatch

		self.pd("Comparing local file '{}' and remote file '{}'".format(
			self._current_file, j['path']))
		self.pd("Local file size : {}".format(self._current_file_size))
		self.pd("Remote file size: {}".format(rsize))

		if self._current_file_size == rsize:
			self.pd("Local and remote file size matches")
			if self._verify:
				if not gotlmd5:
					self._current_file_md5 = md5(self._current_file)
				self.pd("Local file MD5 : {}".format(self._current_file_md5))
				self.pd("Remote file MD5: {}".format(rmd5))

				if self._current_file_md5 == rmd5:
					self.pd("Local and remote file hash matches")
					return const.ENoError
				else:
					pinfo("Local and remote file hash DOESN'T match")
					return const.EHashMismatch
			else:
				return const.ENoError
		else:
			pinfo("Local and remote file size DOESN'T match")
			return const.EHashMismatch

	def _get_file_info_act(self, r, args):
		try:
			remotefile = args[0]
			j = r.json()
			self.jsonq.append(j)
			self.pd("List json: {}".format(j))
			if not 'list' in j:
				return const.EFileNotFound
			args[1] = len(j['list'])
			for f in j['list']:
				if f['path'] == remotefile: # case-sensitive
					self._remote_json = f
					self.pd("File info json: {}".format(self._remote_json))
					return const.ENoError

			return const.EFileNotFound
		except KeyError as ex:
			perr(formatex(ex))
			return const.ERequestFailed

	# the 'meta' command sucks, since it doesn't supply MD5 ...
	# now the JSON is written to self._remote_json, due to Python call-by-reference chaos
	# https://stackoverflow.com/questions/986006/python-how-do-i-pass-a-variable-by-reference
	# as if not enough confusion in Python call-by-reference
	def _get_file_info(self, remotefile, **kwargs):
		if remotefile == const.AppPcsPath: # root path
			# fake it
			rj = {}
			rj['isdir'] = 1
			rj['ctime'] = 0
			rj['fs_id'] = 0
			rj['mtime'] = 0
			rj['path'] = const.AppPcsPath
			rj['md5'] = ''
			rj['size'] = 0
			self._remote_json = rj
			self.pd("File info json: {}".format(self._remote_json))
			return const.ENoError

		rdir, rfile = posixpath.split(remotefile)
		self.pd("__get_file_info(): rdir : {} | rfile: {}".format(rdir, rfile))
        # while refactoring to use _walk_proceed_remote_dir is more dry, it might be less performant -
		# we want to stop once we found the file we need
		if rdir and rfile:
			listStart = 0
			while True:
				pars = {
					'method' : 'list',
					'path' : rdir,
					'by' : 'name', # sort in case we can use binary-search, etc in the futrue.
					'order' : 'asc',
					'limit': '{}-{}'.format(listStart, listStart + const.MaxListEntries)}
				reqargs = [remotefile, 0]
				result = self._get(pcsurl + 'file', pars, self._get_file_info_act, reqargs, **kwargs)
				if result == const.ENoError or reqargs[1] < const.MaxListEntries:
					break
				listStart += const.MaxListEntries
			return result
		else:
			perr("Invalid remotefile '{}' specified.".format(remotefile))
			return const.EArgument

	def get_file_info(self, remotefile = '/'):
		rpath = get_pcs_path(remotefile)
		return self._get_file_info(rpath)

	def _list_act(self, r, args):
		(remotedir, fmt) = args
		j = r.json()
		self.pd("Response: {}".format(j))
		self.jsonq.append(j)
		pr("{} ({}):".format(remotedir, fmt))
		for f in j['list']:
			pr(self._replace_list_format(fmt, f))

		return const.ENoError

	def _proceed_list(self, walkresult, remotepath, dirjs, filejs, args):
		result = walkresult
		fmt = args
		# cutfront = const.AppPcsPathLen
		cutfront = len(remotepath) + 1
		pathlol = [deepcopy(dirjs), deepcopy(filejs)]
		self.pd("Files listl: {}".format(pathlol))
		for path in pathlol:
			for p in path:
				if 'path' in p:
					p['path'] = p['path'][cutfront:]
					pr(self._replace_list_format(fmt, p))
		return result

	def ls(self, remotepath = '',
		fmt = '$t $f $s $m $d',
		sort = 'name', order = 'asc'):
		return self.list(remotepath, fmt, sort, order)

	def list(self, remotepath = '',
		fmt = '$t $f $s $m $d',
		sort = 'name', order = 'asc'):
		''' Usage: list/ls [remotepath] [format] [sort] [order] - list the 'remotepath' directory at Baidu PCS
	remotepath - the remote path at Baidu PCS. default: root directory '/'
	format - specifies how the list are displayed
	  $t - Type: Directory ('D') or File ('F')
	  $f - File name
	  $c - Creation time
	  $m - Modification time
	  $d - MD5 hash
	  $s - Size
	  Because of the interpolation, you need to escape the $ sign,
	  so '\$t - \$f - \$s - \$' will display "Type - File - Size - $'
	  Default format: '$t $f $s $m $d'
		'''
    # sort - sorting by [name, time, size]. default: 'name'
    # order - sorting order [asc, desc]. default: 'asc'
		rpath = get_pcs_path(remotepath)
		pr("{} ({}):".format(rpath, fmt))
		self.file_list = [] # Hacky, but too lazy to make it proper
		return self._walk_proceed_remote_dir(rpath, self._proceed_list, args = fmt, recursive = False)

		# pars = {
		# 	'method' : 'list',
		# 	'path' : rpath,
		# 	'by' : sort,
		# 	'order' : order }

		# return self._get(pcsurl + 'file', pars, self._list_act, (rpath, fmt))

	def _meta_act(self, r, args):
		return self._list_act(r, args)

	def _meta(self, rpath, fmt):
		pars = {
			'method' : 'meta',
			'path' : rpath }
		return self._get(pcsurl + 'file', pars,
			self._meta_act, (rpath, fmt))

	# multi-file meta is not implemented for its low usage
	def meta(self, remotepath = '/', fmt = '$t $u $f $s $c $m $i $b'):
		''' Usage: meta <remotepath> [format] - \
get information of the given path (dir / file) at Baidu Yun.
  remotepath - the remote path
  format - specifies how the list are displayed
    it supports all the format variables in the 'list' command, and additionally the followings:
	$i - fs_id
	$b - MD5 block_list
	$u - Has sub directory or not
'''
		rpath = get_pcs_path(remotepath)
		self.file_list = [] # Hacky, but too lazy to make it proper
		return self._meta(rpath, fmt)

	# this 'is_revision' parameter sometimes gives the following error (e.g. for rapidupload):
	# {u'error_code': 31066, u'error_msg': u'file does not exist'}
	# and maintain it is also an extra burden, so it's disabled for now
	def _add_isrev_param(self, ondup, pars):
		pass
		#if self._isrev and ondup != 'newcopy':
		#	pars['is_revision'] = 1

	def _combine_file_act(self, r, args):
		j = r.json()
		self.jsonq.append(j)
		result = self._verify_current_file(j, False)
		if result == const.ENoError:
			self.pv("'{}' =C=> '{}' OK.".format(self._current_file, args))
		else:
			perr("'{}' =C=> '{}' FAILED.".format(self._current_file, args))
		# save the md5 list, in case we add in resume function later to this program
		self._last_slice_md5s = self._slice_md5s
		self._slice_md5s = []

		return result

	def _combine_file(self, remotepath, ondup = 'overwrite'):
		pars = {
			'method' : 'createsuperfile',
			'path' : remotepath,
			'ondup' : ondup }
		self._add_isrev_param(ondup, pars)

		# always print this, so that we can use these data to combine file later
		pr("Combining the following MD5 slices:")
		for m in self._slice_md5s:
			pr(m)

		param = { 'block_list' : self._slice_md5s }
		return self._post(pcsurl + 'file',
				pars, self._combine_file_act,
				remotepath,
				data = { 'param' : json.dumps(param) } )

	def _upload_slice_act(self, r, args):
		j = r.json()
		# slices must be verified and re-upload if MD5s don't match,
		# otherwise, it makes the uploading slower at the end
		rsmd5 = j['md5']
		self.pd("Uploaded MD5 slice: " + rsmd5)
		if self._current_slice_md5 == rsmd5:
			self._slice_md5s.append(rsmd5)
			self.pv("'{}' >>==> '{}' OK.".format(self._current_file, args))
			return const.ENoError
		else:
			perr("'{}' >>==> '{}' FAILED.".format(self._current_file, args))
			return const.EHashMismatch

	# want to be proper? properness doesn't work
	# there seems to be a bug at Baidu's handling of http text:
	# Content-Disposition: ...  filename=utf-8''yourfile.ext
	# (pass '-ddd' to this program to verify this)
	# when you specify a unicode file name, which will be encoded
	# using the utf-8'' syntax
	# so, we put a work-around here: we always call our file 'file'
	# NOTE: an empty file name '' doesn't seem to work, so we
	# need to give it a name at will, but empty one.
	# apperantly, Baidu PCS doesn't use this file name for
	# checking / verification, so we are probably safe here.
	#files = { 'file' : (os.path.basename(self._current_file), self._current_slice) } )
	#files = { 'file' : (os.path.basename(localpath), f) })
	#files = { 'file' : ('file', f) })
	def _stream_upload(self, fo, pars, act, remotepath,
			url = cpcsurl + 'file',
			uploadfilename = 'file'):
		# https://stackoverflow.com/a/35784072/404271
		form = multipart.encoder.MultipartEncoder({
			'file': (uploadfilename, fo, 'application/octet-stream')})
		headers = {"Content-Type": form.content_type}
		return self._post(url,
			pars, act, remotepath,
			headers=headers, data=form)

	def _upload_slice(self, remotepath):
		pars = {
			'method' : 'upload',
			'type' : 'tmpfile'}

		return self._stream_upload(self._current_slice,
				pars, self._upload_slice_act, remotepath)

	def _update_progress_entry(self, fullpath):
		progress = {}

		try:
			progress = jsonload(self._progresspath)
		except Exception as ex:
			perr("Error loading the progress for: '{}'.\n{}.".format(fullpath, formatex(ex)))

		self.pd("Updating slice upload progress for {}".format(fullpath))
		progress[fullpath] = (self._slice_size, self._slice_md5s)

		try:
			jsondump(progress, self._progresspath, mpsemaphore)
		except Exception as ex:
			perr("Error updating the progress for: '{}'.\n{}.".format(fullpath, formatex(ex)))

	def _delete_progress_entry(self, fullpath):
		try:
			progress = jsonload(self._progresspath)
			# http://stackoverflow.com/questions/11277432/how-to-remove-a-key-from-a-python-dictionary
			#del progress[fullpath]
			self.pd("Removing slice upload progress for {}".format(fullpath))
			progress.pop(fullpath, None)
			jsondump(progress, self._progresspath, mpsemaphore)
		except Exception as ex:
			perr("Error deleting the progress for: '{}'.\n{}.".format(fullpath, formatex(ex)))

	def _upload_file_slices(self, localpath, remotepath, ondup = 'overwrite'):
		pieces = const.MaxSlicePieces
		slice = self._slice_size
		if self._current_file_size <= self._slice_size * const.MaxSlicePieces:
			# slice them using slice size
			pieces = (self._current_file_size + self._slice_size - 1 ) // self._slice_size
		else:
			# the following comparision is done in the caller:
			# elif self._current_file_size <= MaxSliceSize * MaxSlicePieces:

			# no choice, but need to slice them to 'MaxSlicePieces' pieces
			slice = (self._current_file_size + const.MaxSlicePieces - 1) // const.MaxSlicePieces

		self.pd("Slice size: {}, Pieces: {}".format(slice, pieces))

		i = 0
		ec = const.ENoError

		fullpath = os.path.abspath(self._current_file)
		progress = {}
		initial_offset = 0
		# create an empty progress file first
		if not os.path.exists(self._progresspath):
			try:
				jsondump(progress, self._progresspath, mpsemaphore)
			except Exception as ex:
				perr("Error savingprogress, no resumption.\n{}".format(formatex(ex)))

		try:
			progress = jsonload(self._progresspath)
		except Exception as ex:
			perr("Error loading progress, no resumption.\n{}".format(formatex(ex)))

		if fullpath in progress:
			self.pd("Find the progress entry resume uploading")
			(slice, md5s) = progress[fullpath]
			self._slice_md5s = []
			with io.open(self._current_file, 'rb') as f:
				self.pd("Verifying the md5s. Total count = {}".format(len(md5s)))
				for md in md5s:
					cslice = f.read(slice)
					cm = hashlib.md5(cslice)
					if (cm.hexdigest() == md):
						self.pd("{} verified".format(md))
						# TODO: a more rigorous check would be also verifying
						# slices exist at Baidu Yun as well (rapidupload test?)
						# but that's a bit complex. for now, we don't check
						# this but simply delete the progress entry if later
						# we got error combining the slices.
						self._slice_md5s.append(md)
					else:
						break
				self.pd("verified md5 count = {}".format(len(self._slice_md5s)))
			i = len(self._slice_md5s)
			initial_offset = i * slice
			self.pd("Start from offset {}".format(initial_offset))

		with io.open(self._current_file, 'rb') as f:
			start_time = time.time()
			f.seek(initial_offset, os.SEEK_SET)
			while i < pieces:
				self._current_slice = f.read(slice)
				m = hashlib.md5()
				m.update(self._current_slice)
				self._current_slice_md5 = m.hexdigest()
				self.pd("Uploading MD5 slice: {}, #{} / {}".format(
					self._current_slice_md5,
					i + 1, pieces))
				j = 0
				while True:
					ec = self._upload_slice(remotepath)
					if ec == const.ENoError:
						self.pd("Slice MD5 match, continuing next slice")
						pprgr(f.tell(), self._current_file_size, start_time, initial_offset)
						self._update_progress_entry(fullpath)
						break
					elif j < self._retry:
						j += 1
						# TODO: Improve or make it DRY with the _request retry logic
						perr("Slice MD5 mismatch, waiting {} seconds before retrying...".format(const.RetryDelayInSec))
						time.sleep(const.RetryDelayInSec)
						perr("Retrying #{} / {}".format(j + 1, self._retry))
					else:
						self._slice_md5s = []
						break
				if ec != const.ENoError:
					break
				i += 1

		if ec != const.ENoError:
			return ec
		else:
			#self.pd("Sleep 2 seconds before combining, just to be safer.")
			#time.sleep(2)
			ec = self._combine_file(remotepath, ondup = 'overwrite')
			if ec == const.ENoError \
				or ec == const.IESuperfileCreationFailed \
				or ec == const.IEBlockMissInSuperFile2 \
				or ec == const.EMaxRetry: # to handle undocumented error codes if any
				# we delete on success or failure caused by
				# the slices uploaded expired / became invalid
				# (needed for a fresh re-upload later)
				self._delete_progress_entry(fullpath)
			return ec

	def _rapidupload_file_act(self, r, args):
		if self._verify:
			self.pd("Not strong-consistent, sleep 1 second before verification")
			time.sleep(1)
			return self._verify_current_file(r.json(), True)
		else:
			return const.ENoError

	def _rapidupload_file_post(self, rpath, size, md5str, slicemd5str, crcstr, ondup = 'overwrite'):
		pars = {
			'method' : 'rapidupload',
			'path' : rpath,
			'content-length' : size,
			'content-md5' : md5str,
			'slice-md5' : slicemd5str,
			'content-crc32' : crcstr,
			'ondup' : ondup }
		self._add_isrev_param(ondup, pars)

		self.pd("RapidUploading Length: {} MD5: {}, Slice-MD5: {}, CRC: {}".format(
			size, md5str, slicemd5str, crcstr))
		return self._post(pcsurl + 'file', pars, self._rapidupload_file_act)

	def _get_hashes_for_rapidupload(self, lpath, setlocalfile = False):
		if setlocalfile:
			self._current_file = lpath
			self._current_file_size = getfilesize(lpath)

		self._current_file_md5 = md5(self._current_file)
		self._current_file_slice_md5 = slice_md5(self._current_file)
		self._current_file_crc32 = crc32(self._current_file)

	def _rapidupload_file(self, lpath, rpath, ondup = 'overwrite', setlocalfile = False):
		self._get_hashes_for_rapidupload(lpath, setlocalfile)

		md5str = self._current_file_md5
		slicemd5str =  self._current_file_slice_md5
		crcstr = hex(self._current_file_crc32)
		return self._rapidupload_file_post(rpath, self._current_file_size, md5str, slicemd5str, crcstr, ondup)

	def _upload_one_file_act(self, r, args):
		result = self._verify_current_file(r.json(), False)
		if result == const.ENoError:
			self.pv("'{}' ==> '{}' OK.".format(self._current_file, args))
		else:
			perr("'{}' ==> '{}' FAILED.".format(self._current_file, args))

		return result

	def _upload_one_file(self, localpath, remotepath, ondup = 'overwrite'):
		pars = {
			'method' : 'upload',
			'path' : remotepath,
			'ondup' : ondup }
		self._add_isrev_param(ondup, pars)

		with io.open(localpath, 'rb') as f:
			return self._stream_upload(f, pars,
					self._upload_one_file_act, remotepath)

	#TODO: upload empty directories as well?
	def _walk_upload(self, localpath, remotepath, ondup, walk):
		(dirpath, dirnames, filenames) = walk

		rdir = os.path.relpath(dirpath, localpath)
		if rdir == '.':
			rdir = ''
		else:
			rdir = rdir.replace('\\', '/')

		rdir = (remotepath + '/' + rdir).rstrip('/') # '/' bites

		result = const.ENoError
		for name in filenames:
			#lfile = os.path.join(dirpath, name)
			lfile = joinpath(dirpath, name)
			self._current_file = lfile
			self._current_file_size = getfilesize(lfile)
			rfile = rdir + '/' + name.replace('\\', '/')
			# if the corresponding file matches at Baidu Yun, then don't upload
			upload = True
			self._isrev = False
			self._remote_json = {}
			subresult = self._get_file_info(rfile, dumpex = False)
			if subresult == const.ENoError: # same-name remote file exists
				self._isrev = True
				if const.ENoError == self._verify_current_file(self._remote_json, False):
					# the two files are the same
					upload = False
					self.pv("Remote file '{}' already exists, skip uploading".format(rfile))
				else: # the two files are different
					if not self.shalloverwrite("Remote file '{}' exists but is different, "
							"do you want to overwrite it? [y/N]".format(rfile)):
						upload = False
				self._isrev = False

			if upload:
				fileresult = self._upload_file(lfile, rfile, ondup)
				if fileresult != const.ENoError:
					result = fileresult # we still continue
			else:
				pinfo("Remote file '{}' exists and is the same, skip uploading".format(rfile))
				# next / continue

		return result

	def _upload_dir_single(self, localpath, remotepath, ondup = 'overwrite'):
		result = const.ENoError
		# it's so minor that we don't care about the return value
		#self._mkdir(remotepath, retry = False, dumpex = False)
		#for walk in os.walk(localpath, followlinks=self._followlink):
		for walk in self._walk_normal_file(localpath):
			thisresult = self._walk_upload(localpath, remotepath, ondup, walk)
			# we continue even if some upload failed, but keep the last error code
			if thisresult != const.ENoError:
				result = thisresult

		return result

	def _upload_dir_multi(self, localpath, remotepath, ondup = 'overwrite'):
		worker = partial(self._walk_upload, localpath, remotepath, ondup)
		return self._multi_process(worker, self._walk_normal_file(localpath),
			"Direcory Upload")

	def _upload_dir(self, localpath, remotepath, ondup = 'overwrite'):
		self.pd("Uploading directory '{}' to '{}'".format(localpath, remotepath))
		result = const.ENoError
		if Pool and self.processes > 1:
			result = self._upload_dir_multi(localpath, remotepath, ondup)
		else:
			result =  self._upload_dir_single(localpath, remotepath, ondup)
		if result == const.ENoError:
			self._remove_local_on_success(localpath)
		return result

	def _upload_file(self, localpath, remotepath, ondup = 'overwrite'):
		# TODO: this is a quick patch
		if not self._shallinclude(localpath, remotepath, True):
			# since we are not going to upload it, there is no error
			#return const.ENoError
			return const.ESkipped

		self._current_file = localpath
		self._current_file_size = getfilesize(localpath)

		result = const.ENoError
		if self._current_file_size > const.MinRapidUploadFileSize:
			self.pd("'{}' is being RapidUploaded.".format(self._current_file))
			result = self._rapidupload_file(localpath, remotepath, ondup)
			if result == const.ENoError:
				self.pv("RapidUpload: '{}' =R=> '{}' OK.".format(localpath, remotepath))
				self._rapiduploaded = True
			else:
				self._rapiduploaded = False
				if not self._rapiduploadonly:
					self.pd("'{}' can't be RapidUploaded, now trying normal uploading.".format(
						self._current_file))
					# rapid upload failed, we have to upload manually
					if self._current_file_size <= self._slice_size:
						self.pd("'{}' is being non-slicing uploaded.".format(self._current_file))
						# no-slicing upload
						result = self._upload_one_file(localpath, remotepath, ondup)
					elif self._current_file_size <= const.MaxSliceSize * const.MaxSlicePieces:
						# slice them using slice size
						self.pd("'{}' is being slicing uploaded.".format(self._current_file))
						result = self._upload_file_slices(localpath, remotepath, ondup)
					else:
						result = const.EFileTooBig
						perr("Error: size of file '{}' - {} is too big".format(
							self._current_file,
							self._current_file_size))
				else:
					self.pv("'{}' can't be rapidly uploaded, so it's skipped since we are in the rapid-upload-only mode.".format(localpath))
					result = const.ESkipped
		elif self._rapiduploadonly:
			self.pv("'{}' is too small to be rapidly uploaded, so it's skipped since we are in the rapid-upload-only mode.".format(localpath))
			result = const.ESkipped
		else:
			# very small file, must be uploaded manually and no slicing is needed
			self.pd("'{}' is small and being non-slicing uploaded.".format(self._current_file))
			result = self._upload_one_file(localpath, remotepath, ondup)

		if result == const.ENoError:
			self._remove_local_on_success(localpath)
		return result

	def upload(self, localpath = '', remotepath = '', ondup = "overwrite"):
		''' Usage: upload [localpath] [remotepath] [ondup] - \
upload a file or directory (recursively)
    localpath - local path, is the current directory '.' if not specified
    remotepath - remote path at Baidu Yun (after app root directory at Baidu Yun)
    ondup - what to do upon duplication ('overwrite' or 'newcopy'), default: 'overwrite'
		'''
		# copying since Python is call-by-reference by default,
		# so we shall not modify the passed-in parameters
		lpath = localpath.rstrip('\\/ ') # no trailing slashes
		lpathbase = os.path.basename(lpath)
		rpath = remotepath
		if not lpath:
			# so, if you don't specify the local path, it will always be the current directory
			# and thus isdir(localpath) is always true
			lpath = os.path.abspath(".")
			self.pd("localpath not set, set it to current directory '{}'".format(localpath))

		if os.path.isfile(lpath):
			self.pd("Uploading file '{}'".format(lpath))
			if not rpath or rpath == '/': # to root we go
				rpath = lpathbase
			if rpath[-1] == '/': # user intends to upload to this DIR
				rpath = get_pcs_path(rpath + lpathbase)
			else:
				rpath = get_pcs_path(rpath)
				# avoid uploading a file and destroy a directory by accident
				subresult = self._get_file_info(rpath)
				if subresult == const.ENoError: # remote path exists, check is dir or file
					if self._remote_json['isdir']: # do this only for dir
						rpath += '/' + lpathbase # rpath is guaranteed no '/' ended
			self.pd("remote path is '{}'".format(rpath))
			return self._upload_file(lpath, rpath, ondup)
		elif os.path.isdir(lpath):
			self.pd("Uploading directory '{}' recursively".format(lpath))
			rpath = get_pcs_path(rpath)
			return self._upload_dir(lpath, rpath, ondup)
		else:
			perr("Error: invalid local path '{}' for uploading specified.".format(localpath))
			return const.EParameter

	# The parameter 'localfile' is a bit kluge as it carries double meanings,
	# but this is to be command line friendly (one can not input an empty string '' from the command line),
	# so let's just leave it like this unless we can devise a cleverer / clearer weay
	def combine(self, remotefile, localfile = '*', *args):
		''' Usage: combine <remotefile> [localfile] [md5s] - \
try to create a file at PCS by combining slices, having MD5s specified
  remotefile - remote file at Baidu Yun (after app root directory at Baidu Yun)
  localfile - local file to verify against, passing in a star '*' or '/dev/null' means no verification
  md5s - MD5 digests of the slices, can be:
    - list of MD5 hex strings separated by spaces
    - a string in the form of 'l<path>' where <path> points to a text file containing MD5 hex strings separated by spaces or line-by-line
		'''
		self._slice_md5s = []
		if args:
			if args[0].upper() == 'L':
				try:
					with io.open(args[1:], 'r', encoding = 'utf-8') as f:
						contents = f.read()
						digests = filter(None, contents.split())
						for d in digests:
							self._slice_md5s.append(d)
				except IOError as ex:
					perr("Exception occured while reading file '{}'.\n{}".format(
						localfile, formatex(ex)))
			else:
				for arg in args:
					self._slice_md5s.append(arg)
		else:
			perr("You MUST provide the MD5s hex strings through arguments or a file.")
			return const.EArgument

		original_verify = self._verify
		if not localfile or localfile == '*' or localfile == '/dev/null':
			self._current_file = '/dev/null' # Force no verify
			self._verify = False
		else:
			self._current_file = localfile
			self._current_file_size = getfilesize(localfile)

		result = self._combine_file(get_pcs_path(remotefile))
		self._verify = original_verify
		return result

	# no longer used
	def _get_meta_act(self, r, args):
		parse_ok = False
		j = r.json()
		self.jsonq.append(j)
		if 'list' in j:
			lj = j['list']
			if len(lj) > 0:
				self._remote_json = lj[0] # TODO: ugly patch
				# patch for inconsistency between 'list' and 'meta' json
				#self._remote_json['md5'] = self._remote_json['block_list'].strip('[]"')
				self.pd("self._remote_json: {}".format(self._remote_json))
				parse_ok = True
				return const.ENoError

		if not parse_ok:
			self._remote_json = {}
			perr("Invalid JSON:\n{}".format(j))
			return const.EInvalidJson

	# no longer used
	def _get_meta(self, remotefile):
		pars = {
			'method' : 'meta',
			'path' : remotefile }
		return self._get(
			pcsurl + 'file', pars,
			self._get_meta_act)

	# NO LONGER IN USE
	def _downfile_act(self, r, args):
		rfile, offset = args
		with io.open(self._current_file, 'r+b' if offset > 0 else 'wb') as f:
			if offset > 0:
				f.seek(offset)

			rsize = self._remote_json['size']
			start_time = time.time()
			for chunk in r.iter_content(chunk_size = self._dl_chunk_size):
				if chunk: # filter out keep-alive new chunks
					f.write(chunk)
					f.flush()
					pprgr(f.tell(), rsize, start_time)
					# https://stackoverflow.com/questions/7127075/what-exactly-the-pythons-file-flush-is-doing
					#os.fsync(f.fileno())

		# No exception above, then everything goes fine
		result = const.ENoError
		if self._verify:
			self._current_file_size = getfilesize(self._current_file)
			result = self._verify_current_file(self._remote_json, False)

		if result == const.ENoError:
			self.pv("'{}' <== '{}' OK".format(self._current_file, rfile))
		else:
			perr("'{}' <== '{}' FAILED".format(self._current_file, rfile))

		return result

	def _downchunks_act(self, r, args):
		rfile, offset, rsize, start_time = args

		expectedBytes = self._dl_chunk_size
		if rsize - offset < self._dl_chunk_size:
			expectedBytes = rsize - offset

		if len(r.content) != expectedBytes:
			return const.ERequestFailed
		else:
			with io.open(self._current_file, 'r+b' if offset > 0 else 'wb') as f:
				if offset > 0:
					f.seek(offset)

				f.write(r.content)
				pos = f.tell()
				print(pos, rsize, start_time, self._existing_size)
				pprgr(pos, rsize, start_time, existing = self._existing_size)
				if pos - offset == expectedBytes:
					return const.ENoError
				else:
					return const.EFileWrite

	def _down_aria2c(self, remotefile, localfile):
		url = "{}{}".format(dpcsurl, "file")
		# i think encoding in UTF-8 before escaping is presumably the best practice
		# http://stackoverflow.com/a/913653/404271
		pars = {
			"method": "download",
			"path": remotefile.encode('utf-8'),
			"access_token": self._access_token,
			}
		full_url = "{}?{}".format(url, ulp.urlencode(pars))
		cmd = ['aria2c', '--user-agent="{}"'.format(const.UserAgent)] \
			+ shlex.split(self._downloader_args) \
			+ ['-o', localfile, full_url]

		tries = self._retry
		subret = 0
		i = 0
		while True:
			self.pd("call: {}".format(cmd))
			subret = subprocess.call(cmd)
			self.pd("aria2c exited with status: {}".format(subret))
			if subret == 0:
				return const.ENoError
			i += 1
			if i < tries:
				# algo changed: delay more after each failure
				delay = const.RetryDelayInSec * i
				perr("Waiting {} seconds before retrying...".format(delay))
				time.sleep(delay)
				perr("Request Try #{} / {}".format(i + 1, tries))
			else:
				perr("Maximum number ({}) of tries failed.".format(tries))
				if self._quit_when_fail:
					sys.exit(const.EMaxRetry)
				return const.EMaxRetry

	# requirement: self._remote_json is already gotten
	def _downchunks(self, rfile, start):
		rsize = self._remote_json['size']

		pars = {
			'method' : 'download',
			# Do they cause some side effects?
			#'app_id': 250528,
			#'check_blue' : '1',
			#'ec' : '1',
			'path' : rfile }

		offset = start
		self._existing_size = offset
		start_time = time.time()
		while True:
			nextoffset = offset + self._dl_chunk_size
			if nextoffset < rsize:
				headers = { "Range" : "bytes={}-{}".format(
					offset, nextoffset - 1) }
			elif offset > 0:
				headers = { "Range" : "bytes={}-".format(offset) }
			elif rsize >= 1: # offset == 0
				# Fix chunked + gzip response,
				# seems we need to specify the Range for the first chunk as well:
				# https://github.com/houtianze/bypy/pull/161
				#headers = { "Range" : "bytes=0-".format(rsize - 1) }
				headers = { "Range" : "bytes=0-{}".format(rsize - 1) }
			else:
				headers = {}

			# this _may_ solve #163: { "error_code":31326, "error_msg":"anti hotlinking"}
#			if 'Range' in headers:
#				rangemagic = base64.standard_b64encode(headers['Range'][6:].encode('utf-8'))
#				self.pd("headers['Range'][6:]: {} {}".format(headers['Range'][6:], rangemagic))
#				#pars['ru'] = rangemagic

			subresult = self._get(dpcsurl + 'file', pars,
				self._downchunks_act, (rfile, offset, rsize, start_time), headers = headers, cookies = self._cookies)
			if subresult != const.ENoError:
				return subresult

			if nextoffset < rsize:
				offset += self._dl_chunk_size
			else:
				break

		# No exception above, then everything goes fine
		result = const.ENoError
		if self._verify:
			self._current_file_size = getfilesize(self._current_file)
			result = self._verify_current_file(self._remote_json, False)

		if result == const.ENoError:
			self.pv("'{}' <== '{}' OK".format(self._current_file, rfile))
		else:
			perr("'{}' <== '{}' FAILED".format(self._current_file, rfile))

		return result

	def _downfile(self, remotefile, localfile):
		pinfo('{} <- {}'.format(localfile, remotefile))
		# TODO: this is a quick patch
		if not self._shallinclude(localfile, remotefile, False):
			# since we are not going to download it, there is no error
			#return const.ENoError
			return const.ESkipped

		result = const.ENoError
		rfile = remotefile

		self._remote_json = {}
		self.pd("Downloading '{}' as '{}'".format(rfile, localfile))
		self._current_file = localfile
		#if self._verify or self._resumedownload:
		self.pd("Getting info of remote file '{}' for later verification".format(rfile))
		result = self._get_file_info(rfile)
		if result != const.ENoError:
			return result

		offset = 0
		self.pd("Checking if we already have the copy locally")
		if os.path.isfile(localfile):
			self.pd("Same-name local file '{}' exists, checking if contents match".format(localfile))
			self._current_file_size = getfilesize(self._current_file)
			if const.ENoError == self._verify_current_file(self._remote_json, False) \
				and not (self._downloader[:5] == const.DownloaderAria2 and os.path.exists(localfile + '.aria2')):
				self.pd("Same local file '{}' already exists, skip downloading".format(localfile))
				self._remove_remote_on_success(remotefile)
				return const.ENoError
			else:
				if not self.shalloverwrite("Same-name locale file '{}' exists but is different, "
						"do you want to overwrite it? [y/N]".format(localfile)):
					pinfo("Same-name local file '{}' exists but is different, skip downloading".format(localfile))
					#return const.ENoError
					return const.ESkipped

			if self._resumedownload and \
				self._compare_size(self._current_file_size, self._remote_json) == 2:
				if self._resumedl_revertcount < 0:
					if self._current_file_size:
						offset = self._current_file_size
				else:
					# revert back at least self._resumedl_revertcount download chunk(s), default: one
					pieces = self._current_file_size // self._dl_chunk_size
					if pieces > self._resumedl_revertcount:
						offset = (pieces - self._resumedl_revertcount) * self._dl_chunk_size
		elif os.path.isdir(localfile):
			if not self.shalloverwrite("Same-name directory '{}' exists, "
				"do you want to remove it? [y/N]".format(localfile)):
				pinfo("Same-name directory '{}' exists, skip downloading".format(localfile))
				#return const.ENoError
				return const.ESkipped

			self.pv("Directory with the same name '{}' exists, removing ...".format(localfile))
			result = removedir(localfile, self.verbose)
			if result == const.ENoError:
				self.pv("Removed")
			else:
				perr("Error removing the directory '{}'".format(localfile))
				return result

		ldir, file = os.path.split(localfile)
		if ldir and not os.path.exists(ldir):
			result = makedir(ldir, verbose = self.verbose)
			if result != const.ENoError:
				perr("Fail to make directory '{}'".format(ldir))
				return result

		if self._downloader[:5] == const.DownloaderAria2:
			result = self._down_aria2c(rfile, localfile)
		else:
			result = self._downchunks(rfile, offset)

		if result == const.ENoError:
			self._remove_remote_on_success(remotefile)

		return result

	def downfile(self, remotefile, localpath = ''):
		''' Usage: downfile <remotefile> [localpath] - \
download a remote file.
  remotefile - remote file at Baidu Yun (after app root directory at Baidu Yun)
  localpath - local path.
    if it ends with '/' or '\\', it specifies the local directory
    if it specifies an existing directory, it is the local directory
    if not specified, the local directory is the current directory '.'
    otherwise, it specifies the local file name
To stream a file using downfile, you can use the 'mkfifo' trick with omxplayer etc.:
  mkfifo /tmp/omx
  bypy.py downfile <remotepath> /tmp/omx &
  omxplayer /tmp/omx
		'''
		localfile = localpath
		if not localpath:
			localfile = os.path.basename(remotefile)
		elif localpath[-1] == '\\' or \
			localpath[-1] == '/' or \
			os.path.isdir(localpath):
			#localfile = os.path.join(localpath, os.path.basename(remotefile))
			localfile = joinpath(localpath, os.path.basename(remotefile))
		else:
			localfile = localpath

		# import ipdb; ipdb.set_trace()
		pcsrpath = get_pcs_path(remotefile)
		return self._downfile(pcsrpath, localfile)

	def _stream_act_actual(self, r, args):
		pipe, csize = args
		with io.open(pipe, 'wb') as f:
			for chunk in r.iter_content(chunk_size = csize):
				if chunk: # filter out keep-alive new chunks
					f.write(chunk)
					f.flush()
					# https://stackoverflow.com/questions/7127075/what-exactly-the-pythons-file-flush-is-doing
					#os.fsync(f.fileno())

	def _streaming_act(self, r, args):
		return self._stream_act_actual(r, args)

	# NOT WORKING YET
	def streaming(self, remotefile, localpipe, fmt = 'M3U8_480_360', chunk = 4 * const.OneM):
		''' Usage: stream <remotefile> <localpipe> [format] [chunk] - \
stream a video / audio file converted to M3U format at cloud side, to a pipe.
  remotefile - remote file at Baidu Yun (after app root directory at Baidu Yun)
  localpipe - the local pipe file to write to
  format - output video format (M3U8_320_240 | M3U8_480_224 | \
M3U8_480_360 | M3U8_640_480 | M3U8_854_480). Default: M3U8_480_360
  chunk - chunk (initial buffering) size for streaming (default: 4M)
To stream a file, you can use the 'mkfifo' trick with omxplayer etc.:
  mkfifo /tmp/omx
  bypy.py downfile <remotepath> /tmp/omx &
  omxplayer /tmp/omx
  *** NOT WORKING YET ****
		'''
		pars = {
			'method' : 'streaming',
			'path' : get_pcs_path(remotefile),
			'type' : fmt }

		return self._get(pcsurl + 'file', pars,
			self._streaming_act, (localpipe, chunk), stream = True)

	def _walk_proceed_remote_dir_act(self, r, args):
		dirjs, filejs = args
		j = r.json()
		if 'list' not in j:
			self.pd("Key 'list' not found in the response of directory listing request:\n{}".format(j))
			return const.ERequestFailed

		paths = j['list']
		for path in paths:
			if path['isdir']:
				dirjs.append(path)
			else:
				filejs.append(path)

		return const.ENoError

	def _walk_remote_dir(self, remotepath, remoterootpath, args = None, skip_remote_only_dirs = False, recursive = True):
		dirjs = []
		filejs = []
		listStart = 0
		# https://github.com/houtianze/bypy/issues/285
		while True:
			pars = {
				'method' : 'list',
				'path' : remotepath,
				'by' : 'name',
				'order' : 'asc',
				'limit' : '{}-{}'.format(listStart, listStart + const.MaxListEntries)}
			# Python parameters are by-reference and mutable, so they are 'out' by default
			result = self._get(pcsurl + 'file', pars, self._walk_proceed_remote_dir_act, (dirjs, filejs))
			if len(dirjs) + len(filejs) - listStart < const.MaxListEntries:
				break
			listStart += const.MaxListEntries
		yield [result, remotepath, dirjs, filejs, args]
		if result == const.ENoError:
			self.pd("Remote dirs: {}".format(dirjs))
			self.pd("Remote files: {}".format(filejs))
			if recursive:
				for dirj in dirjs:
					crpath = dirj['path'] # crpath - current remote path
					if skip_remote_only_dirs and remoterootpath != None and \
						self._local_dir_contents.get(posixpath.relpath(crpath, remoterootpath)) == None:
						self.pd("Skipping remote-only sub-directory '{}'.".format(crpath))
						continue

					# http://stackoverflow.com/a/8991864/404271
					# for Python 3.3+, "yield from" reads better
					for y in self._walk_remote_dir(crpath, remoterootpath, args, skip_remote_only_dirs):
						yield y

	def _walk_proceed_remote_dir(self, remotepath, proceed, args = None, skip_remote_only_dirs = False, recursive = True):
		result = const.ENoError
		for walk in self._walk_remote_dir(remotepath, remotepath, args, skip_remote_only_dirs, recursive):
			subresult = proceed(*walk)
			if subresult != const.ENoError:
				result = subresult
		return result

	def _prepare_local_dir(self, localdir):
		result = const.ENoError
		if os.path.isfile(localdir):
			result = removefile(localdir, self.verbose)

		if result == const.ENoError:
			if localdir and not os.path.exists(localdir):
				result = makedir(localdir, verbose = self.verbose)

		return result

	def _proceed_downdir(self, walkresult, remotepath, dirjs, filejs, args):
		result = walkresult
		rootrpath, localpath = args
		#rlen = len(remotepath) + 1
		rootlen = len(rootrpath) + 1 # '+ 1' for the trailing '/', it bites.

		subresult = self._prepare_local_dir(localpath)
		if subresult != const.ENoError:
			perr("Fail to prepare local directory '{}' for downloading, ABORT!".format(localpath))
			return subresult

		for dirj in dirjs:
			rdir = dirj['path']
			reldir = rdir[rootlen:]
			#ldir = os.path.join(localpath, reldir)
			ldir = joinpath(localpath, reldir)
			subresult = self._prepare_local_dir(ldir)
			if subresult != const.ENoError:
				perr("Fail to prepare local directory '{}' for downloading, ABORT!".format(ldir))
				return subresult

		for filej in filejs:
			rfile = filej['path']
			relfile = rfile[rootlen:]
			#lfile = os.path.join(localpath, relfile)
			lfile = joinpath(localpath, relfile)
			subresult = self._downfile(rfile, lfile)
			if subresult != const.ENoError:
				result = subresult

		return result

	def _downdir_single(self, rpath, lpath):
		return self._walk_proceed_remote_dir(rpath, self._proceed_downdir, (rpath, lpath))

	def _proceed_downdir_worker(self, arglist):
		return self._proceed_downdir(*arglist)

	def _downdir_multi(self, rpath, lpath):
		return self._multi_process(self._proceed_downdir_worker,
			self._walk_remote_dir(rpath, rpath, (rpath, lpath)),
			"Directory Download")

	def _downdir(self, rpath, lpath):
		result = const.ENoError
		if Pool and self.processes > 1:
			result = self._downdir_multi(rpath, lpath)
		else:
			result = self._downdir_single(rpath, lpath)
		if result == const.ENoError:
			self._remove_remote_on_success(rpath)
		return result

	def downdir(self, remotepath = None, localpath = None):
		''' Usage: downdir [remotedir] [localdir] - \
download a remote directory (recursively)
  remotedir - remote directory at Baidu Yun (after app root directory), if not specified, it is set to the root directory at Baidu Yun
  localdir - local directory. if not specified, it is set to the current directory
		'''
		rpath = get_pcs_path(remotepath)
		lpath = localpath
		if not lpath:
			lpath = '' # empty string does it, no need '.'
		lpath = lpath.rstrip('/\\ ')
		return self._downdir(rpath, lpath)

	def download(self, remotepath = '/', localpath = ''):
		''' Usage: download [remotepath] [localpath] - \
download a remote directory (recursively) / file
  remotepath - remote path at Baidu Yun (after app root directory), if not specified, it is set to the root directory at Baidu Yun
  localpath - local path. if not specified, it is set to the current directory
		'''
		subr = self.get_file_info(remotepath)
		if const.ENoError == subr:
			if 'isdir' in self._remote_json:
				if self._remote_json['isdir']:
					return self.downdir(remotepath, localpath)
				else:
					return self.downfile(remotepath, localpath)
			else:
				perr("Malformed path info JSON '{}' returned".format(self._remote_json))
				return const.EFatal
		elif const.EFileNotFound == subr:
			perr("Remote path '{}' does not exist".format(remotepath))
			return subr
		else:
			perr("Error {} while getting info for remote path '{}'".format(subr, remotepath))
			return subr

	def _mkdir_act(self, r, args):
		if self.verbose:
			j = r.json()
			self.jsonq.append(j)
			pr("path, ctime, mtime, fs_id")
			pr("{path}, {ctime}, {mtime}, {fs_id}".format(**j))

		return const.ENoError

	def _mkdir(self, rpath, **kwargs):
		# TODO: this is a quick patch
		# the code still works because Baidu Yun doesn't require
		# parent directory to exist remotely to upload / create a file
		if not self._shallinclude('.', rpath, True):
			#return const.ENoError
			return const.ESkipped

		self.pd("Making remote directory '{}'".format(rpath))

		pars = {
			'method' : 'mkdir',
			'path' : rpath }
		return self._post(pcsurl + 'file', pars, self._mkdir_act, **kwargs)

	def mkdir(self, remotepath):
		''' Usage: mkdir <remotedir> - \
create a directory at Baidu Yun
  remotedir - the remote directory
'''
		rpath = get_pcs_path(remotepath)
		return self._mkdir(rpath)

	def _move_act(self, r, args):
		j = r.json()
		self.jsonq.append(j)
		list = j['extra']['list']
		fromp = list[0]['from']
		to = list[0]['to']
		self.pd("Remote move: '{}' =mm-> '{}' OK".format(fromp, to))

	# aliases
	def mv(self, fromp, to):
		return self.move(fromp, to)

	def rename(self, fromp, to):
		return self.move(fromp, to)

	def ren(self, fromp, to):
		return self.move(fromp, to)

	def move(self, fromp, to):
		''' Usage: move/mv/rename/ren <from> <to> - \
move a file / dir remotely at Baidu Yun
  from - source path (file / dir)
  to - destination path (file / dir)
		'''
		frompp = get_pcs_path(fromp)
		top = get_pcs_path(to)
		pars = {
			'method' : 'move',
			'from' : frompp,
			'to' : top }

		self.pd("Remote moving: '{}' =mm=> '{}'".format(fromp, to))
		return self._post(pcsurl + 'file', pars, self._move_act)

	def _copy_act(self, r, args):
		j = r.json()
		self.jsonq.append(j)
		for list in j['extra']['list']:
			fromp = list['from']
			to = list['to']
			self.pd("Remote copy: '{}' =cc=> '{}' OK".format(fromp, to))

		return const.ENoError

	# alias
	def cp(self, fromp, to):
		return self.copy(fromp, to)

	def copy(self, fromp, to):
		''' Usage: copy/cp <from> <to> - \
copy a file / dir remotely at Baidu Yun
  from - source path (file / dir)
  to - destination path (file / dir)
		'''
		frompp = get_pcs_path(fromp)
		top = get_pcs_path(to)
		pars = {
			'method' : 'copy',
			'from' : frompp,
			'to' : top }

		self.pd("Remote copying '{}' =cc=> '{}'".format(frompp, top))
		return self._post(pcsurl + 'file', pars, self._copy_act)

	def _delete_act(self, r, args):
		j = r.json()
		self.jsonq.append(j)
		rid = j['request_id']
		if rid:
			pr("Deletion request '{}' OK".format(rid))
			pr("Use 'list' command to confirm")

			return const.ENoError
		else:
			perr("Deletion failed")
			return const.EFailToDeleteFile

	def _delete(self, rpath):
		pars = {
			'method' : 'delete',
			'path' : rpath }

		self.pd("Remote deleting: '{}'".format(rpath))
		return self._post(pcsurl + 'file', pars, self._delete_act)

	def _delete_children_act(self, r, args):
		result = const.ENoError
		j = r.json()
		for f in j['list']:
			# we continue even if some upload failed, but keep the last error code
			thisresult = self._delete(f['path'])
			if thisresult != const.ENoError:
				result = thisresult

		return result

	def _delete_children(self, rpath):
		pars = {
			'method' : 'list',
			'path' : rpath}

		return self._get(pcsurl + 'file', pars, self._delete_children_act, None)

	# aliases
	def remove(self, remotepath):
		return self.delete(remotepath)

	def rm(self, remotepath):
		return self.delete(remotepath)

	def delete(self, remotepath):
		''' Usage: delete/remove/rm <remotepath> - \
delete a file / dir remotely at Baidu Yun
  remotepath - destination path (file / dir)
		'''
		rpath = get_pcs_path(remotepath)
		#if is_pcs_root_path(rpath):
		#	return self._delete_children(rpath)
		#else:
		return self._delete(rpath)

	def _search_act(self, r, args):
		j = r.json()
		self.jsonq.append(j)
		print_pcs_list(j)
		return const.ENoError

	def search(self, keyword, remotepath = None, recursive = True):
		''' Usage: search <keyword> [remotepath] [recursive] - \
search for a file using keyword at Baidu Yun
  keyword - the keyword to search
  remotepath - remote path at Baidu Yun, if not specified, it's app's root directory
  resursive - search recursively or not. default is true
		'''
		rpath = get_pcs_path(remotepath)

		pars = {
			'method' : 'search',
			'path' : rpath,
			'wd' : keyword,
			're' : '1' if str2bool(recursive) else '0'}

		self.pd("Searching: '{}'".format(rpath))
		return self._get(pcsurl + 'file', pars, self._search_act)

	def _listrecycle_act(self, r, args):
		print_pcs_list(r.json())
		return const.ENoError

	def listrecycle(self, start = 0, limit = 1000):
		''' Usage: listrecycle [start] [limit] - \
list the recycle contents
  start - starting point, default: 0
  limit - maximum number of items to display. default: 1000
		'''
		pars = {
			'method' : 'listrecycle',
			'start' : str2int(start),
			'limit' : str2int(limit) }

		self.pd("Listing recycle '{}'")
		return self._get(pcsurl + 'file', pars, self._listrecycle_act)

	def _restore_act(self, r, args):
		path = args
		pr("'{}' found and restored".format(path))
		return const.ENoError

	def _restore_search_act(self, r, args):
		path = args
		flist = r.json()['list']
		fsid = None
		for f in flist:
			if os.path.normpath(f['path'].lower()) == os.path.normpath(path.lower()):
				fsid = f['fs_id']
				self.pd("fs_id for restoring '{}' found".format(fsid))
				break
		if fsid:
			pars = {
				'method' : 'restore',
				'fs_id' : fsid }
			return self._post(pcsurl + 'file', pars, self._restore_act, path)
		else:
			perr("'{}' not found in the recycle bin".format(path))

	def restore(self, remotepath):
		''' Usage: restore <remotepath> - \
restore a file from the recycle bin
  remotepath - the remote path to restore
		'''
		rpath = get_pcs_path(remotepath)
		# by default, only 1000 items, more than that sounds a bit crazy
		pars = {
			'method' : 'listrecycle' }

		self.pd("Searching for fs_id to restore")
		return self._get(pcsurl + 'file', pars, self._restore_search_act, rpath)

	def _proceed_local_gather(self, dirlen, walk):
		#names.sort()
		(dirpath, dirnames, filenames) = walk

		files = []
		for name in filenames:
			#fullname = os.path.join(dirpath, name)
			fullname = joinpath(dirpath, name)
			# ignore broken symbolic links
			if not os.path.exists(fullname):
				self.pd("Local path '{}' does not exist (broken symbolic link?)".format(fullname))
				continue
			files.append((name, getfilesize(fullname), md5(fullname)))

		reldir = dirpath[dirlen:].replace('\\', '/')
		place = self._local_dir_contents.get(reldir)
		for dir in dirnames:
			place.add(dir, PathDictTree('D'))
		for file in files:
			place.add(file[0], PathDictTree('F', size = file[1], md5 = file[2]))

		return const.ENoError

	def _gather_local_dir(self, dir):
		self._local_dir_contents = PathDictTree()
		#for walk in os.walk(dir, followlinks=self._followlink):
		for walk in self._walk_normal_file(dir):
			self._proceed_local_gather(len(dir), walk)
		self.pd(self._local_dir_contents)

	def _proceed_remote_gather(self, walkresult, remotepath, dirjs, filejs, args = None):
		# NOTE: the '+ 1' is due to the trailing slash '/'
		# be careful about the trailing '/', it bit me once, bitterly
		rootrdir = args
		rootlen = len(rootrdir)
		dlen = len(remotepath) + 1
		for d in dirjs:
			self._remote_dir_contents.get(remotepath[rootlen:]).add(
				d['path'][dlen:], PathDictTree('D', size = d['size'], md5 = d['md5'] if 'md5' in d else ''))

		# Baidu made another fuck up here:
		# f['md5'] doesn't have the correct MD5 value, but f['block_list'][0] has
		# This makes no sense, and I'm not going to change the correct code to adapt its wrong behaviors
		# --- Code below for reference ---
		# fmd5 = f['md5']
		# bl = 'block_list'
		# if bl in f and f[bl]:
		# 	fmd5 = f[bl][0]
		# f['path'][dlen:], PathDictTree('F', size = f['size'], md5 = fmd5))
		for f in filejs:
			self._remote_dir_contents.get(remotepath[rootlen:]).add(
				f['path'][dlen:], PathDictTree('F', size = f['size'], md5 = f['md5']))

		return walkresult

	def _gather_remote_dir(self, rdir, skip_remote_only_dirs = False):
		self._remote_dir_contents = PathDictTree()
		self._walk_proceed_remote_dir(rdir, self._proceed_remote_gather, rdir, skip_remote_only_dirs)
		self.pd("---- Remote Dir Contents ---")
		self.pd(self._remote_dir_contents)

	def _compare(self, remotedir = None, localdir = None, skip_remote_only_dirs = False):
		if not localdir:
			localdir = '.'

		self.pv("Gathering local directory ...")
		self._gather_local_dir(localdir)
		self.pv("Done")
		self.pv("Gathering remote directory ...")
		self._gather_remote_dir(remotedir, skip_remote_only_dirs)
		self.pv("Done")
		self.pv("Comparing ...")
		# list merge, where Python shines
		commonsame = []
		commondiff = []
		localonly = []
		remoteonly = []
		# http://stackoverflow.com/questions/1319338/combining-two-lists-and-removing-duplicates-without-removing-duplicates-in-orig
		lps = self._local_dir_contents.allpath()
		rps = self._remote_dir_contents.allpath()
		dps = set(rps) - set(lps)
		allpath = lps + list(dps)
		for p in allpath:
			local = self._local_dir_contents.get(p)
			remote = self._remote_dir_contents.get(p)
			if local is None: # must be in the remote dir, since p is from allpath
				remoteonly.append((remote.type, p))
			elif remote is None:
				localonly.append((local.type, p))
			else: # all here
				same = False
				if local.type == 'D' and remote.type == 'D':
					type = 'D'
					same = True
				elif local.type == 'F' and remote.type == 'F':
					type = 'F'
					if local.extra['size'] == remote.extra['size'] and \
						(not self._verify or local.extra['md5'] == remote.extra['md5']):
						same = True
					else:
						same = False
				else:
					type = local.type + remote.type
					same = False

				if same:
					commonsame.append((type, p))
				else:
					commondiff.append((type, p))

		self.pv("Done")
		return commonsame, commondiff, localonly, remoteonly

	def compare(self, remotedir = None, localdir = None, skip_remote_only_dirs = False):
		''' Usage: compare [remotedir] [localdir] - \
compare the remote directory with the local directory
  remotedir - the remote directory at Baidu Yun (after app's directory). \
if not specified, it defaults to the root directory.
  localdir - the local directory, if not specified, it defaults to the current directory.
  skip_remote_only_dirs - skip remote-only sub-directories (faster if the remote \
directory is much larger than the local one). it defaults to False.
		'''
		same, diff, local, remote = self._compare(get_pcs_path(remotedir), localdir, str2bool(skip_remote_only_dirs))

		pr("==== Same files ===")
		for c in same:
			pr("{} - {}".format(c[0], c[1]))

		pr("==== Different files ===")
		for d in diff:
			pr("{} - {}".format(d[0], d[1]))

		pr("==== Local only ====")
		for l in local:
			pr("{} - {}".format(l[0], l[1]))

		pr("==== Remote only ====")
		for r in remote:
			pr("{} - {}".format(r[0], r[1]))

		pr("\nStatistics:")
		pr("--------------------------------")
		pr("Same: {}".format(len(same)))
		pr("Different: {}".format(len(diff)))
		pr("Local only: {}".format(len(local)))
		pr("Remote only: {}".format(len(remote)))

		self.result['same'] = same
		self.result['diff'] = diff
		self.result['local'] = local
		self.result['remote'] = remote

		return const.ENoError

	def _syncdown_diff_one(self, rpath, localdir, d):
		result = const.ENoError
		t = d[0]
		p = d[1]
		#lcpath = os.path.join(localdir, p) # local complete path
		lcpath = joinpath(localdir, p) # local complete path
		rcpath = rpath + '/' + p # remote complete path
		if t == 'DF':
			result = removedir(lcpath, self.verbose)
			subresult = self._downfile(rcpath, lcpath)
			if subresult != const.ENoError:
				result = subresult
		elif t == 'FD':
			result = removefile(lcpath, self.verbose)
			subresult = makedir(lcpath, verbose = self.verbose)
			if subresult != const.ENoError:
				result = subresult
		else: # " t == 'F' " must be true
			result = self._downfile(rcpath, lcpath)

		return result

	def _syncdown_remote_one(self, rpath, localdir, r):
		result = const.ENoError
		t = r[0]
		p = r[1]
		#lcpath = os.path.join(localdir, p) # local complete path
		lcpath = joinpath(localdir, p) # local complete path
		rcpath = rpath + '/' + p # remote complete path
		if t == 'F':
			subresult = self._downfile(rcpath, lcpath)
			if subresult != const.ENoError:
				result = subresult
		else: # " t == 'D' " must be true
			subresult = makedir(lcpath, verbose = self.verbose)
			if subresult != const.ENoError:
				result = subresult

		return result

	def _syncdown_delete_local(self, localdir, local):
		result = const.ENoError
		for l in local:
			# use os.path.isfile()/isdir() instead of l[0], because we need to check file/dir existence.
			# as we may have removed the parent dir previously during the iteration
			#p = os.path.join(localdir, l[1])
			p = joinpath(localdir, l[1])
			if os.path.isfile(p):
				subresult = removefile(p, self.verbose)
				if subresult != const.ENoError:
					result = subresult
			elif os.path.isdir(p):
				subresult = removedir(p, self.verbose)
				if subresult != const.ENoError:
					result = subresult

		return result

	def _syncdown_single(self, rpath, localdir, compare_result):
		result = const.ENoError
		same, diff, local, remote = compare_result
		# clear the way
		for d in diff:
			subresult = self._syncdown_diff_one(rpath, localdir, d)
			if subresult != const.ENoError:
				result = subresult
		for r in remote:
			subresult = self._syncdown_remote_one(rpath, localdir, r)
			if subresult != const.ENoError:
				result = subresult
		return result

	def _syncdown_multi(self, rpath, localdir, compare_result):
		result = const.ENoError
		same, diff, local, remote = compare_result
		# clear the way
		worker = partial(self._syncdown_diff_one, rpath, localdir)
		subresult = self._multi_process(worker, diff, "Sync down")
		if subresult != const.ENoError:
			result = subresult
		worker = partial(self._syncdown_remote_one, rpath, localdir)
		subresult = self._multi_process(worker, remote, "")
		if subresult != const.ENoError:
			result = subresult
		return result

	def syncdown(self, remotedir = '', localdir = '', deletelocal = False):
		''' Usage: syncdown [remotedir] [localdir] [deletelocal] - \
sync down from the remote directory to the local directory
  remotedir - the remote directory at Baidu Yun (after app's directory) to sync from. \
if not specified, it defaults to the root directory
  localdir - the local directory to sync to if not specified, it defaults to the current directory.
  deletelocal - delete local files that are not inside Baidu Yun directory, default is False
		'''
		result = const.ENoError
		rpath = get_pcs_path(remotedir)
		compare_result = self._compare(rpath, localdir, False)
		same, diff, local, remote = compare_result
		if Pool and self.processes > 1:
			subresult = self._syncdown_multi(rpath, localdir, compare_result)
			if subresult != const.ENoError:
				result = subresult
		else:
			subresult = self._syncdown_single(rpath, localdir, compare_result)
			if subresult != const.ENoError:
				result = subresult
		if str2bool(deletelocal):
			subresult = self._syncdown_delete_local(localdir, local)
			if subresult != const.ENoError:
				result = subresult

		if result == const.ENoError:
			self._remove_remote_on_success(rpath)
		return result

	def _syncup_diff_one(self, rpath, localdir, d):
		result = const.ENoError
		t = d[0] # type
		p = d[1] # path
		#lcpath = os.path.join(localdir, p) # local complete path
		lcpath = joinpath(localdir, p) # local complete path
		rcpath = rpath + '/' + p # remote complete path
		if self.shalloverwrite("Do you want to overwrite '{}' at Baidu Yun? [y/N]".format(p)):
			# this path is before get_pcs_path() since delete() expects so.
			#result = self.delete(rpartialdir + '/' + p)
			result = self._delete(rcpath)
#				self.pd("diff type: {}".format(t))
#				self._isrev = True
#				if t != 'F':
#					result = self.move(remotedir + '/' + p, remotedir + '/' + p + '.moved_by_bypy.' + time.strftime("%Y%m%d%H%M%S"))
#					self._isrev = False
			if t == 'F' or t == 'FD':
				subresult = self._upload_file(lcpath, rcpath)
				if subresult != const.ENoError:
					result = subresult
			else: # " t == 'DF' " must be true
				subresult = self._mkdir(rcpath)
				if subresult != const.ENoError and subresult != const.IEFileAlreadyExists:
					result = subresult
		else:
			pinfo("Uploading '{}' skipped".format(lcpath))

		return result

	def _syncup_local_one(self, rpath, localdir, l):
		result = const.ENoError
		t = l[0]
		p = l[1]
		#lcpath = os.path.join(localdir, p) # local complete path
		lcpath = joinpath(localdir, p) # local complete path
		rcpath = rpath + '/' + p # remote complete path
		self.pd("local type: {}".format(t))
		self._isrev = False
		if t == 'F':
			subresult = self._upload_file(lcpath, rcpath)
			if subresult != const.ENoError:
				result = subresult
		else: # " t == 'D' " must be true
			subresult = self._mkdir(rcpath)
			if subresult != const.ENoError and subresult != const.IEFileAlreadyExists:
				result = subresult

		return result

	def _syncup_delete_remote(self, rpath, remote):
		result = const.ENoError
		# i think the list is built top-down, so directories appearing later are either
		# children or another set of directories
		pp = '\\' # previous path, setting to '\\' make sure it won't be found in the first step
		for r in remote:
			#p = rpartialdir + '/' + r[1]
			p = rpath + '/' + r[1]
			if 0 != p.find(pp): # another path
				#subresult = self.delete(p)
				subresult = self._delete(p)
				if subresult != const.ENoError:
					result = subresult
			pp = p

		return result

	def _syncup_single(self, localdir, rpath, compare_result):
		result = const.ENoError
		same, diff, local, remote = compare_result
		# clear the way
		for d in diff:
			subresult = self._syncup_diff_one(rpath, localdir, d)
			if subresult != const.ENoError:
				result = subresult
		for l in local:
			subresult = self._syncup_local_one(rpath, localdir, l)
			if subresult != const.ENoError:
				result = subresult
		return result

	def _syncup_multi(self, localdir, rpath, compare_result):
		result = const.ENoError
		same, diff, local, remote = compare_result
		# clear the way
		worker = partial(self._syncup_diff_one, rpath, localdir)
		subresult = self._multi_process(worker, diff, "Sync up")
		if subresult != const.ENoError:
			result = subresult
		worker = partial(self._syncup_local_one, rpath, localdir)
		subresult = self._multi_process(worker, local, "")
		if subresult != const.ENoError:
			result = subresult
		return result

	def syncup(self, localdir = '', remotedir = '', deleteremote = False):
		''' Usage: syncup [localdir] [remotedir] [deleteremote] - \
sync up from the local directory to the remote directory
  localdir - the local directory to sync from if not specified, it defaults to the current directory.
  remotedir - the remote directory at Baidu Yun (after app's directory) to sync to. \
if not specified, it defaults to the root directory
  deleteremote - delete remote files that are not inside the local directory, default is False
		'''
		result = const.ENoError
		rpath = get_pcs_path(remotedir)
		#rpartialdir = remotedir.rstrip('/ ')
		compare_result = self._compare(rpath, localdir, True)
		same, diff, local, remote = compare_result
		if Pool and self.processes > 1:
			subresult = self._syncup_multi(localdir, rpath, compare_result)
			if subresult != const.ENoError:
				result = subresult
		else:
			subresult = self._syncup_single(localdir, rpath, compare_result)
			if subresult != const.ENoError:
				result = subresult
		if str2bool(deleteremote):
			subresult = self._syncup_delete_remote(rpath, remote)
			if subresult != const.ENoError:
				result = subresult
		if result == const.ENoError:
			self._remove_local_on_success(localdir)
		return result

	def dumpcache(self):
		''' Usage: dumpcache - display file hash cache'''
		if cached.cacheloaded:
			#pprint.pprint(cached.cache)
			MyPrettyPrinter().pprint(cached.cache)
			return const.ENoError
		else:
			perr("Cache not loaded.")
			return const.ECacheNotLoaded

	def cleancache(self):
		''' Usage: cleancache - remove invalid entries from hash cache file'''
		if os.path.exists(self._hashcachepath):
			try:
				# backup first
				backup = self._hashcachepath + '.lastclean'
				shutil.copy(self._hashcachepath, backup)
				self.pd("Hash Cache file '{}' backed up as '{}".format(
					self._hashcachepath, backup))
				cached.cleancache()
				return const.ENoError
			except Exception as ex:
				perr(formatex(ex))
				return const.EException
		else:
			return const.EFileNotFound

	def _cdl_act(self, r, args):
		try:
			pr(pprint.pformat(r.json()))
			return const.ENoError
		except:
			pr(pprint.pformat({ 'text': rb(r.text) }))
			return const.IETaskNotFound

	def _prepare_cdl_add(self, source_url, rpath, timeout):
		pr("Adding cloud download task:")
		pr("{} =cdl=> {}".format(source_url, rpath))
		pars = {
			'method': 'add_task',
			'source_url': source_url,
			'save_path': rpath,
			'timeout': 3600 }
		return pars

	def _cdl_add(self, source_url, rpath, timeout):
		pars = self._prepare_cdl_add(source_url, rpath, timeout)
		return self._post(pcsurl + 'services/cloud_dl', pars, self._cdl_act)

	def _get_cdl_dest(self, source_url, save_path):
		rpath = get_pcs_path(save_path)
		# download to /apps/bypy root
		if rpath == const.AppPcsPath \
			or (const.ENoError == self._get_file_info(rpath) \
				and self._remote_json['isdir']):
			filename = source_url.split('/')[-1]
			rpath += '/' + filename
		return rpath

	def cdl_add(self, source_url, save_path = '/', timeout = 3600):
		''' Usage: cdl_add <source_url> [save_path] [timeout] - add an offline (cloud) download task
  source_url - the URL to download file from.
  save_path - path on PCS to save file to. default is to save to root directory '/'.
  timeout - timeout in seconds. default is 3600 seconds.
		'''
		rpath = self._get_cdl_dest(source_url, save_path)
		return self._cdl_add(source_url, rpath, timeout)

	def _get_cdl_query_pars(self, task_ids, op_type):
		pars = {
			'method': 'query_task',
			'task_ids': task_ids,
			'op_type': op_type}
		return pars

	def _cdl_query(self, task_ids, op_type):
		pars =  self._get_cdl_query_pars(task_ids, op_type)
		return self._post(pcsurl + 'services/cloud_dl', pars, self._cdl_act)

	def cdl_query(self, task_ids, op_type = 1):
		''' Usage: cdl_query <task_ids>  - query existing offline (cloud) download tasks
  task_ids - task ids seperated by comma (,).
  op_type - 0 for task info; 1 for progress info. default is 1
		'''
		return self._cdl_query(task_ids, op_type)

	def _cdl_mon_act(self, r, args):
		try:
			task_id, start_time, done = args
			j = r.json()
			ti = j['task_info'][str(task_id)]
			if ('file_size' not in ti) or ('finished_size' not in ti):
				done[0] = True
				pr(j)
			else:
				total = int(ti['file_size'])
				finish = int(ti['finished_size'])
				done[0] = (total != 0 and (total == finish))
				pprgr(finish, total, start_time)
				if done[0]:
					pr(pprint.pformat(j))
			return const.ENoError
		except Exception as ex:
			perr("Exception while monitoring offline (cloud) download task:\n{}".format(formatex(ex)))
			perr("Baidu returned:\n{}".format(rb(r.text)))
			return const.EInvalidJson

	def _cdl_addmon_act(self, r, args):
		try:
			args[0] = r.json()
			pr(pprint.pformat(args[0]))
			return const.ENoError
		except Exception as ex:
			perr("Exception while adding offline (cloud) download task:\n{}".format(formatex(ex)))
			perr("Baidu returned:\n{}".format(rb(r.text)))
			return const.EInvalidJson

	def _cdl_sighandler(self, signum, frame):
		pr("Cancelling offline (cloud) download task: {}".format(self._cdl_task_id))
		result = self._cdl_cancel(self._cdl_task_id)
		pr("Result: {}".format(result))
		sys.exit(const.EAbort)

	def _cdl_addmon(self, source_url, rpath, timeout = 3600):
		pars = self._prepare_cdl_add(source_url, rpath, timeout)
		jc = [{}] # out param
		result = self._post(pcsurl + 'services/cloud_dl',
			pars, self._cdl_addmon_act, jc)
		if result == const.ENoError:
			if not 'task_id' in jc[0]:
				return const.EInvalidJson
			task_id = jc[0]['task_id']
			pars = self._get_cdl_query_pars(task_id, 1)
			start_time = time.time()
			done = [ False ] # out param
			# cancel task on Ctrl-C
			pr("Press Ctrl-C to cancel the download task")
			self._cdl_task_id = task_id
			setsighandler(signal.SIGINT, self._cdl_sighandler)
			setsighandler(signal.SIGHUP, self._cdl_sighandler)
			try:
				while True:
					result = self._post(
						pcsurl + 'services/cloud_dl', pars, self._cdl_mon_act,
						(task_id, start_time, done))
					if result == const.ENoError:
						if done[0] == True:
							return const.ENoError
					else:
						return result
					time.sleep(5)
			except KeyboardInterrupt:
				pr("Canceling offline (cloud) downloa task: {}".format(task_id))
				self._cdl_cancel(task_id)
				return const.EAbort
		else:
			return result

	def cdl_addmon(self, source_url, save_path = '/', timeout = 3600):
		''' Usage: cdl_addmon <source_url> [save_path] [timeout] - add an offline (cloud) download task and monitor the download progress
  source_url - the URL to download file from.
  save_path - path on PCS to save file to. default is to save to root directory '/'.
  timeout - timeout in seconds. default is 3600 seconds.
		'''
		rpath = self._get_cdl_dest(source_url, save_path)
		return self._cdl_addmon(source_url, rpath, timeout)

	def _cdl_list(self):
		pars = {
			'method': 'list_task' }
		return self._post(pcsurl + 'services/cloud_dl', pars, self._cdl_act)

	def cdl_list(self):
		''' Usage: cdl_list - list offline (cloud) download tasks
		'''
		return self._cdl_list()

	def _cdl_cancel(self, task_id):
		pars = {
			'method': 'cancel_task',
			'task_id': task_id }
		return self._post(pcsurl + 'services/cloud_dl', pars, self._cdl_act)

	def cdl_cancel(self, task_id):
		''' Usage: cdl_cancel <task_id>  - cancel an offline (cloud) download task
  task_id - id of the task to be canceled.
		'''
		return self._cdl_cancel(task_id)

	def _get_accept_cmd(self, rpath):
		md5str = self._current_file_md5
		slicemd5str =  self._current_file_slice_md5
		crcstr = hex(self._current_file_crc32)
		remotepath = rpath[const.AppPcsPathLen:]
		if len(remotepath) == 0:
			remotepath = 'PATH_NAME_MISSING'
		cmd = "bypy accept {} {} {} {} {}".format(
			remotepath, self._current_file_size, md5str, slicemd5str, crcstr)
		return cmd

	def _share_local_file(self, lpath, rpath, fast):
		filesize = getfilesize(lpath)
		if filesize < const.MinRapidUploadFileSize:
			perr("File size ({}) of '{}' is too small (must be greater or equal than {}) to be shared".format(
				human_size(filesize), lpath, human_size(const.MinRapidUploadFileSize)))
			return const.EParameter

		if fast:
			self._get_hashes_for_rapidupload(lpath, setlocalfile = True)
			pr(self._get_accept_cmd(rpath))
			return const.ENoError

		ulrpath = const.RemoteTempDir + '/' + posixpath.basename(lpath)
		result = self._upload_file(lpath, ulrpath)
		if result != const.ENoError:
			perr("Unable to share as uploading failed")
			return result

		if not self._rapiduploaded:
			i = 0
			while i < const.ShareRapidUploadRetries:
				i += 1
				result = self._rapidupload_file(lpath, ulrpath, setlocalfile = True)
				if result == const.ENoError: # or result == IEMD5NotFound: # retrying if MD5 not found _may_ make the file available?
					break
				else:
					self.pd("Retrying #{} for sharing '{}'".format(i, lpath))
					time.sleep(1)

		if result == const.ENoError:
			pr(self._get_accept_cmd(rpath))
			return const.ENoError
		elif result == const.IEMD5NotFound:
			pr("# Sharing (RapidUpload) not possible for '{}', error: {}".format(lpath, result))
			return result
		else:
			pr("# Error sharing '{}', error: {}".format(lpath, result))
			return result

	def _share_local_dir(self, lpath, rpath, fast):
		result = const.ENoError
		for walk in self._walk_normal_file(lpath):
			(dirpath, dirnames, filenames) = walk
			for filename in filenames:
				rpart = os.path.relpath(dirpath, lpath)
				if rpart == '.':
					rpart = ''
				subr = self._share_local_file(
					joinpath(dirpath, filename),
					posixpath.join(rpath, rpart, filename),
					fast)
				if subr != const.ENoError:
					result = subr
		return result

	# assuming the caller asks once only
	def _ok_to_use_remote_temp_dir(self):
		if const.SettingKey_OverwriteRemoteTempDir in self._setting and \
		self._setting[const.SettingKey_OverwriteRemoteTempDir]:
			return True

		# need to check existence of the remote temp dir
		fir = self._get_file_info(const.RemoteTempDir)
		if fir == const.ENoError: # path exists
			msg = '''
In order to use this functionality, we need to use a temporary directory '{}' \
at Baidu Cloud Storage. However, this remote path exists already.
Is it OK to empty this directory? (Unless coincidentally you uploaded files there, \
it's probably safe to allow this)
y/N/a (yes/NO/always)?
'''.format(const.RemoteTempDir).strip()
			ans = ask(msg).lower()
			if len(ans) >= 0:
				a = ans[0]
				if a == 'y':
					return True
				elif a == 'a':
					# persist
					self._setting[const.SettingKey_OverwriteRemoteTempDir] = True
					self.savesetting()
					return True
				else:
					return False
			else:
				return False
		elif fir == const.EFileNotFound:
			return True
		elif fir == const.ERequestFailed:
			perr("Request to get info of the remote temp dir '{}' failed, can't continue.".format(const.RemoteTempDir))
			return False
		else:
			assert 0 == "Future work handling (more return code) needed"
			return False

	def _share_local(self, lpath, rpath, fast):
		if not os.path.exists(lpath):
			perr("Local path '{}' does not exist.".format(lpath))
			return const.EParameter

		if not self._ok_to_use_remote_temp_dir():
			perr("Can't continue unless you allow the program to use the remote temporary directory '{}'".format(const.RemoteTempDir))
			return const.EUserRejected

		if fast:
			pr("# fast (unverified) sharing, no network I/O needed (for local sharing), but the other person may not be able to accept some of your files")
		if os.path.isfile(lpath):
			# keep the same file name
			lname = os.path.basename(lpath)
			rfile = joinpath(rpath, lname, '/')
			return self._share_local_file(lpath, rfile, fast)
		elif os.path.isdir(lpath):
			return self._share_local_dir(lpath, rpath, fast)
		else:
			perr("Local path '{}' is not a file or directory.".format(lpath))
			return const.EParameter

	def _share_remote_file(self, tmpdir, rpath, srpath, fast):
		rdir, rfile = posixpath.split(rpath)
		lpath = joinpath(tmpdir, rfile)
		subr = self._downfile(rpath, lpath)
		if subr != const.ENoError:
			perr("Fatal: Error {} while downloading remote file '{}'".format(subr, rpath))
			return subr

		return self._share_local_file(lpath, srpath, fast)

	def _proceed_share_remote(self, walkresult, rpath, dirjs, filejs, args):
		remoterootlen, tmpdir, srpath, fast = args
		result = walkresult
		for filej in filejs:
			rfile = filej['path']
			subr = self._share_remote_file(tmpdir, rfile, joinpath(srpath, rfile[remoterootlen:], sep = '/'), fast)
			if subr != const.ENoError:
				result = subr
		return result

	def _share_remote_dir(self, tmpdir, rpath, srpath, fast):
		return self._walk_proceed_remote_dir(rpath, self._proceed_share_remote, (len(rpath), tmpdir, srpath, fast))

	def _share_remote(self, tmpdir, rpath, srpath, fast): # srpath - share remote path (full path)
		subr = self._get_file_info(rpath)
		if const.ENoError == subr:
			if 'isdir' in self._remote_json:
				if self._remote_json['isdir']:
					return self._share_remote_dir(tmpdir, rpath, srpath, fast)
				else:
					return self._share_remote_file(tmpdir, rpath, srpath, fast)
			else:
				perr("Malformed path info JSON '{}' returned".format(self._remote_json))
				return const.EFatal
		elif const.EFileNotFound == subr:
			perr("Remote path '{}' does not exist".format(rpath))
			return subr
		else:
			perr("Error {} while getting info for remote path '{}'".format(subr, rpath))
			return subr

	def share(self, path = '.', sharepath = '/', islocal = True, fast = False):
		islocal = str2bool(islocal)
		fast = str2bool(fast)
		if islocal:
			lpath = path
			rpath = get_pcs_path(sharepath)
			result = self._share_local(lpath, rpath, fast)
			if not fast:
				# not critical
				self._delete(const.RemoteTempDir)
			return result
		else:
			rpath = get_pcs_path(path)
			srpath = get_pcs_path(sharepath)
			tmpdir = tempfile.mkdtemp(prefix = 'bypy_')
			self.pd("Using local temporary directory '{}' for sharing".format(tmpdir))
			try:
				result = self._share_remote(tmpdir, rpath, srpath, fast)
			except Exception as ex:
				result = const.EFatal
				perr("Exception while sharing remote path '{}'.\n{}".format(
					rpath, formatex(ex)))
			finally:
				removedir(tmpdir)
			return result

	def _accept(self, rpath, size, md5str, slicemd5str, crcstr):
		# we have no file to verify against
		verify = self._verify
		self._verify = False
		result = self._rapidupload_file_post(rpath, size, md5str, slicemd5str, crcstr)
		self._verify = verify
		if result == const.ENoError:
			self.pv("Accepted: {}".format(rpath))
		else:
			perr("Unable to accept: {}, Error: {}".format(rpath, result))
		return result

	def accept(self, remotepath, size, md5str, slicemd5str, crcstr):
		rpath = get_pcs_path(remotepath)
		return self._accept(rpath, size, md5str, slicemd5str, crcstr)

	def _locate_fastest_upload_server_act(self, r, args):
		global cpcsurl
		j = r.json()
		cpcsurl = j['host']
		return const.ENoError

	# public Baidu API, no token needed, and not much useful. #127
	def _locate_fastest_upload_server(self):
		url = const.PcsUrl + 'file'
		pars = { 'method': 'locateupload'}
		return self._get(url, pars, self._locate_fastest_upload_server_act, addtoken = False)

	def _list_pcs_hosts_act(self, r, args):
		j = r.json()
		pr(pprint.pformat(j))
		return const.ENoError

	def _list_pcs_hosts(self):
		url = const.PcsUrl + 'manage'
		pars = { 'method': 'listhost'}
		return self._get(url, pars, self._list_pcs_hosts_act, addtoken = False)

	def _test_speed(self, url, repeats = 1):
		self.pd("Speed testing (GET) ({}x): {}".format(repeats, url))
		start = time.time()
		for i in range(repeats):
			requests.get(url)
		stop = time.time()
		duration = stop - start
		self.pd("Time taken: {:.3f}s".format(duration))
		return duration

	def _select_fastest_mirror_act(self, r, args):
		pr("Selecting fastest mirror")
		j = r.json()
		self.pv("List of PCS mirrors:\n" + pprint.pformat(j))
		if 'path' not in j:
			return const.EInvalidJson
		pcspath = j['path']
		if 'list' not in j:
			return const.EInvalidJson
		hosts = j['list']
		if len(hosts) < 1:
			return const.EInvalidJson
		selected_host = hosts[0]['host']
		shortest_time = float('inf')
		for h in hosts:
			host = h['host']
			try:
				time_taken = self._test_speed('https://' + host + pcspath, 3)
				if time_taken < shortest_time:
					selected_host = host
					shortest_time = time_taken
			except Exception as ex:
				perr("Error while testing speed of {}: {}, skipping.".format(host, formatex(ex)))
				continue
		pr("Selected mirror: " + selected_host)
		self.setpcsurl(selected_host)
		return const.ENoError

	def _select_fastest_mirror(self):
		url = const.PcsUrl + 'manage'
		pars = { 'method': 'listhost'}
		return self._get(url, pars, self._select_fastest_mirror_act, addtoken = False)

def sighandler(signum, frame):
	pr("Signal {} received, Abort".format(signum))
	sys.exit(const.EAbort)

# http://www.gnu.org/software/libc/manual/html_node/Basic-Signal-Handling.html
def setsighandler(signum, handler):
	oldhandler = signal.signal(signum, handler)
	if oldhandler == signal.SIG_IGN:
		signal.signal(signum, signal.SIG_IGN)
	return oldhandler

def setuphandlers():
	if iswindows():
		# setsighandler(signal.CTRL_C_EVENT, sighandler)
		# setsighandler(signal.CTRL_BREAK_EVENT, sighandler)
		# bug, see: http://bugs.python.org/issue9524
		pass
	else:
		setsighandler(signal.SIGBUS, sighandler)
		setsighandler(signal.SIGHUP, sighandler)
		# https://stackoverflow.com/questions/108183/how-to-prevent-sigpipes-or-handle-them-properly
		setsighandler(signal.SIGPIPE, signal.SIG_IGN)
		setsighandler(signal.SIGQUIT, sighandler)
		setsighandler(signal.SIGSYS, sighandler)
	setsighandler(signal.SIGABRT, sighandler)
	setsighandler(signal.SIGFPE, sighandler)
	setsighandler(signal.SIGILL, sighandler)
	setsighandler(signal.SIGINT, sighandler)
	setsighandler(signal.SIGSEGV, sighandler)
	setsighandler(signal.SIGTERM, sighandler)

def getparser():
	#name = os.path.basename(sys.argv[0])
	version = "v%s" % const.__version__
	version_message = '%%(prog)s %s' % (version)
	desc = "{} - {}".format(version_message, const.__desc__)

	# setup argument parser
	epilog = "Commands:\n"
	summary = []
	for k, v in ByPy.__dict__.items():
		if callable(v):
			if v.__doc__:
				help = v.__doc__.strip()
				pos = help.find(const.HelpMarker)
				if pos != -1:
					pos_body = pos + len(const.HelpMarker)
					helpbody = help[pos_body:]
					helpline = helpbody.split('\n')[0].strip() + '\n'
					if helpline.find('help') == 0:
						summary.insert(0, helpline)
					else:
						summary.append(helpline)
					#commands.append(v.__name__) # append command name

	remaining = summary[1:]
	remaining.sort()
	summary = [summary[0]] + remaining
	epilog += ''.join(summary)

	parser = ArgumentParser(
		prog = const.__title__,
		description=desc,
		formatter_class=RawDescriptionHelpFormatter, epilog=epilog)

	# help, version, program information etc
	parser.add_argument('-V', '--version', action='version', version=version_message)

	# debug, logging
	parser.add_argument("-d", "--debug",
		dest="debug", action="count", default=0,
		help="set debugging level (-dd to increase debugging level, -ddd to enable HTPP traffic debugging as well (very talkative)) [default: %(default)s]")
	parser.add_argument("-v", "--verbose", dest="verbose", default=0, action="count", help="set verbosity level [default: %(default)s]")

	# program tunning, configration (those will be passed to class ByPy)
	parser.add_argument("-r", "--retry",
		dest="retry", default=5, type=int,
		help="number of retry attempts on network error [default: %(default)i times]")
	parser.add_argument("-q", "--quit-when-fail",
		dest="quit", action="store_true",
		help="quit when maximum number of retry failed [default: %(default)s]")
	parser.add_argument("-t", "--timeout",
		dest="timeout", default=const.DefaultTimeOutInSeconds, type=float,
		help="network timeout in seconds [default: %(default)s]")
	parser.add_argument("-s", "--slice",
		dest="slice", default=const.DefaultSliceSize,
		help="size of file upload slice (can use '1024', '2k', '3MB', etc) [default: {} MB]".format(const.DefaultSliceInMB))
	parser.add_argument("--chunk",
		dest="chunk", default=const.DefaultDlChunkSize,
		help="size of file download chunk (can use '1024', '2k', '3MB', etc) [default: {} MB]".format(const.DefaultDlChunkSize // const.OneM))
	parser.add_argument("-e", "--verify",
		dest="verify", action="store_true",
		help="verify upload / download [default : %(default)s]")
	parser.add_argument("-f", "--force-hash",
		dest="forcehash", action="store_true",
		help="force file MD5 / CRC32 calculation instead of using cached value")
	parser.add_argument("--no-resume-download",
		dest="resumedl", action="store_false",
		help="resume instead of restarting when downloading if local file already exists [default: %(default)s]")
	parser.add_argument("--include-regex",
		dest="incregex", default='',
		help="regular expression of files to include. if not specified (default), everything is included. for download, the regex applies to the remote files; for upload, the regex applies to the local files. to exclude files, think about your regex, some tips here: https://stackoverflow.com/questions/406230/regular-expression-to-match-string-not-containing-a-word [default: %(default)s]")
	parser.add_argument("--on-dup",
		dest="ondup", default='overwrite',
		help="what to do when the same file / folder exists in the destination: 'overwrite', 'skip', 'prompt' [default: %(default)s]")
	parser.add_argument("--no-symlink",
		dest="followlink", action="store_false",
		help="DON'T follow symbol links when uploading / syncing up")
	parser.add_argument(const.DisableSslCheckOption,
		dest="checkssl", action="store_false",
		help="DON'T verify host SSL cerificate")
	parser.add_argument(const.CaCertsOption,
		dest="cacerts", default=None,
		help="Specify the path for CA Bundle [default: %(default)s]")
	parser.add_argument("--mirror",
		dest="mirror", default='',
		help="Specify the PCS mirror (e.g. bj.baidupcs.com. Open 'https://pcs.baidu.com/rest/2.0/pcs/manage?method=listhost' to get the list) to use. [default: " + const.PcsDomain + "]")
	parser.add_argument("--select-fastest-mirror",
		dest="selectmirror", action="store_true",
		help="Let the program run some tests and select the fastest PCS mirror it detectes. [default: %(default)s]")
	parser.add_argument("--rapid-upload-only",
		dest="rapiduploadonly", action="store_true",
		help="only upload large files that can be rapidly uploaded")
	parser.add_argument("--resume-download-revert-back",
		dest="resumedl_revertcount", default=const.DefaultResumeDlRevertCount,
		type=int, metavar='RCOUNT',
		help="Revert back at least %(metavar)s download chunk(s) and align to chunk boundary when resuming the download. A negative value means NO reverts. [default: %(default)s]")
	parser.add_argument("--move",
		dest="deletesource", action="store_true",
		help="Delete source files/directories after download/upload/syncdown/syncup is successful (This will force verification of the files). [default: %(default)s]")
	if Pool:
		parser.add_argument(const.MultiprocessOption,
			dest="processes", default=const.DefaultProcessCount, type=int,
			help="Number of parallel processes. (Only applies to dir sync/dl/ul). [default: %(default)s]")

	# support aria2c
	parser.add_argument("--downloader",
		dest="downloader", default="aria2",
		help="downloader to use (use python if not specified). valid values: {} [default: %(default)s]".format(const.Downloaders))
	parser.add_argument("--downloader-arguments",
		dest="downloader_args", default="",
		help=("arguments for the downloader:\n"
			"normally, the string is the arguments to be passed to the downloader. "
			"however, when it begins with '{0}', it will be treated as the name of file, "
			"whose contents will be used as the downloader arguments "
			"(example: when specifying '{0}args.txt', file contents of 'args.txt' will be "
			"used as the downloader arguments, not the string '@args.txt' itself).\n"
			"you can also use environment variable '{1}' to specify the downloader arguments "
			"(the environment variable has lower priority compared to this argument).\n"
			"default values: {2}"
			).format(
				const.DownloaderArgsIsFilePrefix,
				const.DownloaderArgsEnvKey,
				const.DownloaderDefaultArgs))

	# expose this to provide a primitive multi-user support
	parser.add_argument("--config-dir", dest="configdir", default=const.ConfigDir, help="specify the config path [default: %(default)s]")

	# action
	parser.add_argument(const.CleanOptionShort, const.CleanOptionLong,
		dest="clean", action="count", default=0,
		help="remove the token file (need re-auth) and upload progress file, "
			"-cc: clean hash cache file as well")

	# the MAIN parameter - what command to perform
	parser.add_argument("command", nargs='*', help = "operations (quota, list, etc)")

	return parser

def clean_prog_files(cleanlevel, verbose, configdir = const.ConfigDir):
	tokenpath = os.path.join(configdir, const.TokenFileName)
	result = removefile(tokenpath, verbose)
	if result == const.ENoError:
		pr("Token file '{}' removed. You need to re-authorize "
		   "the application upon next run".format(tokenpath))
	else:
		perr("Failed to remove the token file '{}'".format(tokenpath))
		perr("You need to remove it manually")

	prgrpath = os.path.join(configdir, const.ProgressFileName)
	subresult = removefile(prgrpath, verbose)
	if result == const.ENoError:
		pr("Progress file '{}' removed, "
			"any upload will be started from the beginning.".format(prgrpath))
	else:
		perr("Failed to remove the progress file '{}'".format(prgrpath))
		result = subresult

	if cleanlevel >= 2:
		cachepath = os.path.join(configdir, const.HashCacheFileName)
		subresult = os.remove(cachepath)
		if subresult == const.ENoError:
			pr("Hash Cache File '{}' removed.".format(cachepath))
		else:
			perr("Failed to remove the Hash Cache File '{}'".format(cachepath))
			perr("You need to remove it manually")
			result = subresult

	return result

def printBaiduBanner():
	banner ='''
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
Baidu PCS currently gives totally WRONG MD5 hash for remote files, and I AM NOT GOING TO ADAPT TO IT!
So if it doesn't work, install version '1.6.10' using the following command:
pip install bypy==1.6.10
Version 1.6.10 has a workaround for this.
### Seeking maintainer for this `bypy` project, if you are interested, please create an issue at github, thanks. ###
----------------------------------------------------------------
百度云盘返回的MD5全错了，鬼知道他们什么时候会改回来！现在代码是按照MD5值是正确的前提来写的，不会改。
所以你发现比较功能等不能正确工作时，请安装 1.6.10 版本：
pip install bypy==1.6.10
1.6.10版有个临时解决方案。
### 寻求这个`bypy`项目维护者，有意的话请去github上建issue，谢谢。###
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
'''
	print(banner)

def main(argv=None): # IGNORE:C0111
	''' Main Entry '''
	# printBaiduBanner()

	by = None
	reqres = check_requirements()
	if reqres == CheckResult.Error:
		perr("Requirement checking failed")
		sys.exit(const.EFatal)

	result = const.ENoError
	if argv is None:
		argv = sys.argv
	else:
		sys.argv.extend(argv)

	setuphandlers()

	parser = getparser()
	args = parser.parse_args()
	dl_args = ''
	if not args.downloader_args:
		if const.DownloaderArgsEnvKey in os.environ:
			dl_args = os.environ[const.DownloaderArgsEnvKey]
	else:
		prefixlen = len(const.DownloaderArgsIsFilePrefix)
		if args.downloader_args[:prefixlen] == const.DownloaderArgsIsFilePrefix: # file
			with io.open(args.downloader_args[prefixlen:], 'r', encoding = 'utf-8') as f:
				dl_args = f.read().strip()
		else:
			dl_args = args.downloader_args

	# house-keeping reminder
	# TODO: may need to move into ByPy for customized config dir
	if os.path.exists(const.HashCachePath):
		cachesize = getfilesize(const.HashCachePath)
		if cachesize > 10 * const.OneM or cachesize == -1:
			pwarn((
				"*** WARNING ***\n"
				"Hash Cache file '{0}' is very large ({1}).\n"
				"This may affect program's performance (high memory consumption).\n"
				"You can first try to run 'bypy.py cleancache' to slim the file.\n"
				"But if the file size won't reduce (this warning persists),"
				" you may consider deleting / moving the Hash Cache file '{0}'\n"
				"*** WARNING ***\n\n\n").format(const.HashCachePath, human_size(cachesize)))

	# check for situations that require no ByPy object creation first
	if args.clean >= 1:
		return clean_prog_files(args.clean, args.verbose, args.configdir)

	# some arguments need some processing
	try:
		slice_size = interpret_size(args.slice)
	except (ValueError, KeyError):
		perr("Error: Invalid slice size specified '{}'".format(args.slice))
		return const.EArgument

	try:
		chunk_size = interpret_size(args.chunk)
	except (ValueError, KeyError):
		perr("Error: Invalid slice size specified '{}'".format(args.slice))
		return const.EArgument

	if len(args.command) <= 0 or \
		(len(args.command) == 1 and args.command[0].lower() == 'help'):
		parser.print_help()
		return const.EArgument
	elif len(args.command) == 2 and args.command[0].lower() == 'help':
		ByPy.help(args.command[1])
		return const.EArgument
	elif args.command[0] in ByPy.__dict__: # dir(ByPy), dir(by)
		#timeout = args.timeout or None

		cached.usecache = not args.forcehash
		bypyopt = {
			'slice_size': slice_size,
			'dl_chunk_size': chunk_size,
			'verify': args.verify,
			'retry': args.retry,
			'timeout': args.timeout,
			'quit_when_fail': args.quit,
			'resumedownload': args.resumedl,
			'incregex': args.incregex,
			'ondup': args.ondup,
			'followlink': args.followlink,
			'checkssl': args.checkssl,
			'cacerts': args.cacerts,
			'rapiduploadonly': args.rapiduploadonly,
			'mirror': args.mirror,
			'selectmirror': args.selectmirror,
			'configdir': args.configdir,
			'resumedl_revertcount': args.resumedl_revertcount,
			'deletesource': args.deletesource,
			'downloader': args.downloader,
			'downloader_args': dl_args,
			'verbose': args.verbose,
			'debug': args.debug}
		if Pool:
			bypyopt['processes'] = args.processes

		# we construct a ByPy object here.
		# if you want to try PanAPI, simply replace ByPy with PanAPI, and all the bduss related function _should_ work
		# I didn't use PanAPI here as I have never tried out those functions inside
		by = ByPy(**bypyopt)
		uargs = []
		for arg in args.command[1:]:
			if sys.version_info[0] < 3:
				uargs.append(unicode(arg, gvar.SystemEncoding))
			else:
				uargs.append(arg)
		result = getattr(by, args.command[0])(*uargs)
		if result != const.ENoError:
			errmsg = '-' * 64 + "\nError {}{}".format(result, ': ' + const.ErrorExplanations[result] if result in const.ErrorExplanations else '')
			perr(errmsg)
	else:
		perr("Error: Command '{}' not available.".format(args.command[0]))
		parser.print_help()
		return const.EParameter

	# just let it spew exceptions so that we have the stack trace on death
	# except KeyboardInterrupt:
	# 	# handle keyboard interrupt
	# 	pr("KeyboardInterrupt")
	# 	pr("Abort")
	# except Exception as ex:
	# 	# NOTE: Capturing the exeption as 'ex' seems to matter, otherwise this:
	# 	# except Exception:
	# 	# will sometimes give exception ...
	# 	perr("Exception occurred:\n{}".format(formatex(ex)))
	# 	pr("Abort")
	#	raise

if __name__ == "__main__":
	main()

# vim: tabstop=4 noexpandtab shiftwidth=4 softtabstop=4 ff=unix fileencoding=utf-8
