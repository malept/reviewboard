# -*- coding: utf-8 -*-
from __future__ import absolute_import, unicode_literals

import logging
import os
import re
from datetime import datetime
from shutil import rmtree
from tempfile import mkdtemp

import pysvn
from pysvn import (ClientError, Revision, opt_revision_kind,
                   SVN_DIRENT_CREATED_REV)

from django.core.cache import cache
from django.utils.translation import ugettext as _
from djblets.util.compat import six
from djblets.util.compat.six.moves.urllib.parse import (urlsplit, urlunsplit,
                                                        quote)

from reviewboard.scmtools.core import (Branch, Commit,
                                       HEAD, PRE_CREATION)
from reviewboard.scmtools.errors import (AuthenticationError,
                                         FileNotFoundError,
                                         SCMError)


class Client(object):
    required_module = 'pysvn'

    AUTHOR_KEYWORDS = ['Author', 'LastChangedBy']
    DATE_KEYWORDS = ['Date', 'LastChangedDate']
    REVISION_KEYWORDS = ['Revision', 'LastChangedRevision', 'Rev']
    URL_KEYWORDS = ['HeadURL', 'URL']
    ID_KEYWORDS = ['Id']
    HEADER_KEYWORDS = ['Header']

    # Mapping of keywords to known aliases
    keywords = {
        # Standard keywords
        'Author':              AUTHOR_KEYWORDS,
        'Date':                DATE_KEYWORDS,
        'Revision':            REVISION_KEYWORDS,
        'HeadURL':             URL_KEYWORDS,
        'Id':                  ID_KEYWORDS,
        'Header':              HEADER_KEYWORDS,

        # Aliases
        'LastChangedBy':       AUTHOR_KEYWORDS,
        'LastChangedDate':     DATE_KEYWORDS,
        'LastChangedRevision': REVISION_KEYWORDS,
        'Rev':                 REVISION_KEYWORDS,
        'URL':                 URL_KEYWORDS,
    }

    def __init__(self, config_dir, repopath, username=None, password=None):
        self.repopath = repopath
        self.client = pysvn.Client(config_dir)

        if username:
            self.client.set_default_username(six.text_type(username))

        if password:
            self.client.set_default_password(six.text_type(password))

    def _do_on_path(self, cb, path, revision=HEAD):
        if not path:
            raise FileNotFoundError(path, revision)

        try:
            normpath = self.normalize_path(path)

            # SVN expects to have URLs escaped. Take care to only
            # escape the path part of the URL.
            if self.client.is_url(normpath):
                pathtuple = urlsplit(normpath)
                path = pathtuple[2]
                if isinstance(path, six.text_type):
                    path = path.encode('utf-8', 'ignore')
                normpath = urlunsplit((pathtuple[0],
                                       pathtuple[1],
                                       quote(path),
                                       '', ''))

            normrev = self.__normalize_revision(revision)
            return cb(normpath, normrev)

        except ClientError as e:
            stre = six.text_type(e)
            if 'File not found' in stre or 'path not found' in stre:
                raise FileNotFoundError(path, revision,
                                        detail=six.text_type(e))
            elif 'callback_ssl_server_trust_prompt required' in stre:
                raise SCMError(
                    _('HTTPS certificate not accepted.  Please ensure that '
                      'the proper certificate exists in %s '
                      'for the user that reviewboard is running as.')
                    % os.path.join(self.config_dir, 'auth'))
            elif 'callback_get_login required' in stre:
                raise AuthenticationError(
                    msg=_('Login to the SCM server failed.'))
            else:
                raise SCMError(e)

    @property
    def branches(self):
        """Returns a list of branches.

        This assumes the standard layout in the repository."""
        results = []

        trunk, unused = self.client.list(self.normalize_path('trunk'),
                                         dirent_fields=SVN_DIRENT_CREATED_REV,
                                         recurse=False)[0]
        results.append(
            Branch('trunk', six.text_type(trunk['created_rev'].number), True))

        try:
            branches = self.client.list(
                self.normalize_path('branches'),
                dirent_fields=SVN_DIRENT_CREATED_REV)[1:]
            for branch, unused in branches:
                results.append(Branch(
                    branch['path'].split('/')[-1],
                    six.text_type(branch['created_rev'].number)))
        except ClientError:
            # It's possible there aren't any branches. Ignore errors for this
            # part.
            pass

        return results

    def get_commits(self, start):
        commits = self.client.log(
            self.repopath,
            revision_start=Revision(opt_revision_kind.number,
                                    int(start)),
            limit=31)

        results = []

        # We fetch one more commit than we care about, because the entries in
        # the svn log don't include the parent revision.
        for i in range(len(commits) - 1):
            commit = commits[i]
            parent = commits[i + 1]

            date = datetime.utcfromtimestamp(commit['date'])
            results.append(Commit(
                commit['author'],
                six.text_type(commit['revision'].number),
                date.isoformat(),
                commit['message'],
                six.text_type(parent['revision'].number)))

        # If there were fewer than 31 commits fetched, also include the last
        # one in the list so we don't leave off the initial revision.
        if len(commits) < 31:
            commit = commits[-1]
            date = datetime.utcfromtimestamp(commit['date'])
            results.append(Commit(
                commit['author'],
                six.text_type(commit['revision'].number),
                date.isoformat(),
                commit['message']))

        return results

    def get_change(self, revision, cache_key):
        """Get an individual change.

        This returns a tuple with the commit message and the diff contents.
        """
        revision = int(revision)
        head_revision = Revision(opt_revision_kind.number, revision)

        commit = cache.get(cache_key)
        if commit:
            message = commit.message
            author_name = commit.author_name
            date = commit.date
            base_revision = Revision(opt_revision_kind.number, commit.parent)
        else:
            commits = self.client.log(
                self.repopath,
                revision_start=head_revision,
                limit=2)
            commit = commits[0]
            message = commit['message']
            author_name = commit['author']
            date = datetime.utcfromtimestamp(commit['date']).\
                isoformat()

            try:
                commit = commits[1]
                base_revision = commit['revision']
            except IndexError:
                base_revision = Revision(opt_revision_kind.number, 0)

        tmpdir = mkdtemp(prefix='reviewboard-svn.')

        diff = self.client.diff(
            tmpdir,
            self.repopath,
            revision1=base_revision,
            revision2=head_revision,
            diff_options=['-u'])

        rmtree(tmpdir)

        commit = Commit(author_name, six.text_type(head_revision.number), date,
                        message, six.text_type(base_revision.number))
        commit.diff = diff
        return commit

    def _get_file_data(self, normpath, normrev):
        data = self.client.cat(normpath, normrev)

        # Find out if this file has any keyword expansion set.
        # If it does, collapse these keywords. This is because SVN
        # will return the file expanded to us, which would break patching.
        keywords = self.client.propget("svn:keywords", normpath, normrev,
                                       recurse=True)
        if normpath in keywords:
            data = self.collapse_keywords(data, keywords[normpath])

        return data

    def get_file(self, path, revision=HEAD):
        return self._do_on_path(self._get_file_data, path, revision)

    def _get_file_keywords(self, normpath, normrev):
        keywords = self.client.propget("svn:keywords", normpath, normrev,
                                       recurse=True)
        return keywords.get(normpath)

    def get_keywords(self, path, revision=HEAD):
        return self._do_on_path(self._get_file_keywords, path, revision)

    def collapse_keywords(self, data, keyword_str):
        """
        Collapse SVN keywords in string.

        SVN allows for several keywords (such as $Id$ and $Revision$) to
        be expanded, though these keywords are limited to a fixed set
        (and associated aliases) and must be enabled per-file.

        Keywords can take two forms: $Keyword$ and $Keyword::     $
        The latter allows the field to take a fixed size when expanded.

        When we cat a file on SVN, the keywords come back expanded, which
        isn't good for us as we need to diff against the collapsed version.
        This function makes that transformation.
        """
        def repl(m):
            if m.group(2):
                return "$%s::%s$" % (m.group(1), " " * len(m.group(3)))

            return "$%s$" % m.group(1)

        # Get any aliased keywords
        keywords = [keyword
                    for name in re.split(r'\W+', keyword_str)
                    for keyword in self.keywords.get(name, [])]

        return re.sub(r"\$(%s):(:?)([^\$\n\r]*)\$" % '|'.join(keywords),
                      repl, data)

    def get_filenames_in_revision(self, revision):
        r = self.__normalize_revision(revision)
        logs = self.client.log(self.repopath, r, r, True)

        if len(logs) == 0:
            return []
        elif len(logs) == 1:
            return [f['path'] for f in logs[0]['changed_paths']]
        else:
            assert False

    def __normalize_revision(self, revision):
        if revision == HEAD:
            r = Revision(opt_revision_kind.head)
        elif revision == PRE_CREATION:
            raise FileNotFoundError('', revision)
        else:
            r = Revision(opt_revision_kind.number, six.text_type(revision))

        return r

    @property
    def repository_info(self):
        try:
            info = self.client.info2(self.repopath, recurse=False)
        except ClientError as e:
            raise SCMError(e)

        return {
            'uuid': info[0][1].repos_UUID,
            'root_url': info[0][1].repos_root_URL,
            'url': info[0][1].URL
        }

    def normalize_path(self, path):
        if path.startswith(self.repopath):
            return path
        elif path.startswith('//'):
            return self.repopath + path[1:]
        elif path[0] == '/':
            return self.repopath + path
        else:
            return self.repopath + "/" + path

    def ssl_certificate(self, path, on_failure=None):
        cert = {}

        def ssl_server_trust_prompt(trust_dict):
            cert.update(trust_dict.copy())
            del cert['failures']
            if on_failure:
                return False, 0, False
            else:
                return True, trust_dict['failures'], True

        self.client.callback_ssl_server_trust_prompt = ssl_server_trust_prompt

        try:
            info = self.client.info2(path, recurse=False)
            logging.debug('SVN: Got repository information for %s: %s' %
                          (path, info))
        except ClientError as e:
            if on_failure:
                on_failure(e, cert)
