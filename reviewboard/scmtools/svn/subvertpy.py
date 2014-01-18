# -*- coding: utf-8 -*-
from __future__ import absolute_import, unicode_literals

import os

try:
    from subvertpy import ra, SubversionException
    from subvertpy.client import Client as SVNClient, get_config
    imported_dependency = True
except ImportError:
    # This try-except block is here for the sole purpose of avoiding
    # exceptions with nose if subvertpy isn't installed when someone runs
    # the testsuite.
    imported_dependency = False

from django.core.cache import cache
from djblets.util.compat import six

from reviewboard.scmtools.core import (Branch, Commit, Revision,
                                       HEAD, PRE_CREATION)
from reviewboard.scmtools.errors import (FileNotFoundError,
                                         SCMError)
from reviewboard.scmtools.svn import base

B = six.binary_type
DIFF_UNIFIED = [B('-u')]
SVN_AUTHOR = B('svn:author')
SVN_DATE = B('svn:date')
SVN_KEYWORDS = B('svn:keywords')
SVN_LOG = B('svn:log')


class Client(base.Client):
    required_module = 'subvertpy'

    def __init__(self, config_dir, repopath, username=None, password=None):
        super(Client, self).__init__(config_dir, repopath, username, password)
        self.repopath = B(self.repopath)
        self.config_dir = B(config_dir)
        self.auth = ra.Auth([ra.get_simple_provider(),
                             ra.get_username_provider()])
        if username:
            self.auth.set_parameter(B('svn:auth:username'), B(username))
        if password:
            self.auth.set_parameter(B('svn:auth:password'), B(password))
        self.ra = ra.RemoteAccess(self.repopath, auth=self.auth)
        cfg = get_config(self.config_dir)
        self.client = SVNClient(cfg, auth=self.auth)

    @property
    def branches(self):
        """Returns a list of branches.

        This assumes the standard layout in the repository."""
        results = []
        try:
            dirents = self.ra.get_dir(B('.'), -1, ra.DIRENT_CREATED_REV)[0]
        except SubversionException as e:
            raise SCMError(e)
        trunk = B('trunk')
        if trunk in dirents:
            created_rev = dirents[trunk]['created_rev']
            results.append(Branch('trunk', six.text_type(created_rev), True))

        try:
            dirents = self.ra.get_dir(B('branches'), -1,
                                      ra.DIRENT_CREATED_REV)[0]
        except SubversionException as e:
            raise SCMError(e)
        for name, prop in six.iteritems(dirents):
            results.append(Branch(six.text_type(name),
                                  six.text_type(dirents[name]['created_rev'])))

        return results

    def get_commits(self, start):
        """Returns a list of commits."""
        results = []

        if start.isdigit():
            start = int(start)
        commits = list(self.ra.iter_log(None, start, end=0, limit=31))
        # We fetch one more commit than we care about, because the entries in
        # the svn log doesn't include the parent revision.
        for i, (_, rev, props, _) in enumerate(commits[:-1]):
            parent = commits[i + 1]
            commit = Commit(props[SVN_AUTHOR], six.text_type(rev),
                            # [:-1] to remove the Z
                            props[SVN_DATE][:-1], props[SVN_LOG],
                            six.text_type(parent[1]))
            results.append(commit)
        return results

    def get_change(self, revision, cache_key):
        """Get an individual change.

        This returns a tuple with the commit message and the diff contents.
        """
        revision = int(revision)

        commit = cache.get(cache_key)
        if commit:
            message = commit.message
            author_name = commit.author_name
            date = commit.date
            base_revision = commit.parent
        else:
            commits = list(self.ra.iter_log(None, revision, 0, limit=2))
            rev, props = commits[0][1:3]
            message = props[SVN_LOG]
            author_name = props[SVN_AUTHOR]
            date = props[SVN_DATE]

            if len(commits) > 1:
                base_revision = commits[1][1]
            else:
                base_revision = 0

        out, err = self.client.diff(base_revision, revision, self.repopath,
                                    self.repopath, diffopts=DIFF_UNIFIED)

        commit = Commit(author_name, six.text_type(revision), date,
                        message, six.text_type(base_revision))
        commit.diff = out.read()
        return commit

    def get_file(self, path, revision=HEAD):
        """Returns the contents of a given file at the given revision."""
        if not path:
            raise FileNotFoundError(path, revision)
        revnum = self._normalize_revision(revision)
        path = B(self.normalize_path(path))
        data = six.StringIO()
        try:
            self.client.cat(path, data, revnum)
        except SubversionException as e:
            raise FileNotFoundError(e)
        contents = data.getvalue()
        keywords = self.get_keywords(path, revision)
        if keywords:
            contents = self.collapse_keywords(contents, keywords)
        return contents

    def get_keywords(self, path, revision=HEAD):
        """Returns a list of SVN keywords for a given path."""
        revnum = self._normalize_revision(revision, negatives_allowed=False)
        path = self.normalize_path(path)
        return self.client.propget(SVN_KEYWORDS, path, None, revnum).get(path)

    def _normalize_revision(self, revision, negatives_allowed=True):
        if revision == HEAD:
            return B('HEAD')
        elif revision == PRE_CREATION:
            raise FileNotFoundError('', revision)
        elif isinstance(revision, Revision):
            revnum = int(revision.name)
        elif isinstance(revision, (B,) + six.string_types):
            revnum = int(revision)
        return revnum

    def get_filenames_in_revision(self, revision):
        """Returns a list of filenames associated with the revision."""
        paths = None

        def log_cb(changed_paths, rev, props, has_children=False):
            paths = changed_paths
        revnum = self._normalize_revision(revision)
        self.client.log(log_cb, self.repopath, revnum, revnum, limit=1,
                        discover_changed_paths=True)
        if paths:
            return paths.keys()
        else:
            return []

    @property
    def repository_info(self):
        """Returns metadata about the repository:

        * UUID
        * Root URL
        * URL
        """
        try:
            base = os.path.basename(self.repopath)
            info = self.client.info(self.repopath, 'HEAD')[base]
        except SubversionException as e:
            raise SCMError(e)

        return {
            'uuid': info.repos_uuid,
            'root_url': info.repos_root_url,
            'url': info.url
        }

    def ssl_certificate(self, path, on_failure=None):
        '''Not sure we need this in subvertpy...'''
