# -*- coding: utf-8 -*-
from __future__ import unicode_literals

import re

from reviewboard.scmtools.core import HEAD


class Client(object):
    '''Base SVN client.'''

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

    @property
    def branches(self):
        """Returns a list of branches.

        This assumes the standard layout in the repository."""
        raise NotImplementedError

    def get_commits(self, start):
        raise NotImplementedError

    def get_change(self, revision, cache_key):
        raise NotImplementedError

    def get_file(self, path, revision=HEAD):
        raise NotImplementedError

    def get_keywords(self, path, revision=HEAD):
        raise NotImplementedError

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
        raise NotImplementedError

    @property
    def repository_info(self):
        raise NotImplementedError

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
        raise NotImplementedError
