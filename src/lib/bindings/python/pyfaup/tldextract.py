# -*- coding: utf-8 -*-
"""`tldextract` accurately separates the gTLD or ccTLD (generic or country code
top-level domain) from the registered domain and subdomains of a URL.

    >>> import tldextract
    >>> tldextract.extract('http://forums.news.cnn.com/')
    ExtractResult(subdomain='forums.news', domain='cnn', tld='com')
    >>> tldextract.extract('http://forums.bbc.co.uk/') # United Kingdom
    ExtractResult(subdomain='forums', domain='bbc', tld='co.uk')
    >>> tldextract.extract('http://www.worldbank.org.kg/') # Kyrgyzstan
    ExtractResult(subdomain='www', domain='worldbank', tld='org.kg')

`ExtractResult` is a namedtuple, so it's simple to access the parts you want.

    >>> ext = tldextract.extract('http://forums.bbc.co.uk')
    >>> ext.domain
    'bbc'
    >>> '.'.join(ext[:2]) # rejoin subdomain and domain
    'forums.bbc'
"""


from functools import wraps
from operator import itemgetter
import errno
import logging
import os
import re
import socket
import sys
import urllib.request, urllib.error, urllib.parse
import urllib.parse

try:
    import pickle as pickle
except ImportError:
    import pickle

try:
    import pkg_resources
except ImportError:
    class pkg_resources(object):
        """Fake pkg_resources interface which falls back to getting resources
        inside `tldextract`'s directory.
        """
        @classmethod
        def resource_stream(cls, package, resource_name):
            moddir = os.path.dirname(__file__)
            f = os.path.join(moddir, resource_name)
            return open(f)

LOG = logging.getLogger("tldextract")

SCHEME_RE = re.compile(r'^([' + urllib.parse.scheme_chars + ']+:)?//')
IP_RE = re.compile(r'^(([0-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-5])\.){3}([0-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-5])$')

class ExtractResult(tuple):
    'ExtractResult(subdomain, domain, tld)'
    __slots__ = ()
    _fields = ('subdomain', 'domain', 'tld')

    def __new__(cls, subdomain, domain, tld):
        'Create new instance of ExtractResult(subdomain, domain, tld)'
        return tuple.__new__(cls, (subdomain, domain, tld))

    @classmethod
    def _make(cls, iterable, new=tuple.__new__, len=len):
        'Make a new ExtractResult object from a sequence or iterable'
        result = new(cls, iterable)
        if len(result) != 3:
            raise TypeError('Expected 3 arguments, got %d' % len(result))
        return result

    def __repr__(self):
        'Return a nicely formatted representation string'
        return 'ExtractResult(subdomain=%r, domain=%r, tld=%r)' % self

    def _asdict(self):
        'Return a new dict which maps field names to their values'
        return dict(list(zip(self._fields, self)))

    def _replace(self, **kwds):
        'Return a new ExtractResult object replacing specified fields with new values'
        result = self._make(list(map(kwds.pop, ('subdomain', 'domain', 'tld'), self)))
        if kwds:
            raise ValueError('Got unexpected field names: %r' % list(kwds.keys()))
        return result

    def __getnewargs__(self):
        'Return self as a plain tuple.  Used by copy and pickle.'
        return tuple(self)

    subdomain = property(itemgetter(0), doc='Alias for field number 0')
    domain = property(itemgetter(1), doc='Alias for field number 1')
    tld = property(itemgetter(2), doc='Alias for field number 2')

class TLDExtract(object):
    def __init__(self, fetch=True, cache_file=''):
        """
        Constructs a callable for extracting subdomain, domain, and TLD
        components from a URL.

        If fetch is True (the default) and no cached TLD set is found, this
        extractor will fetch TLD sources live over HTTP on first use. Set to
        False to not make HTTP requests. Either way, if the TLD set can't be
        read, the module will fall back to the included TLD set snapshot.

        Specifying cache_file will override the location of the TLD set.
        Defaults to /path/to/tldextract/.tld_set.

        """
        self.fetch = fetch
        self.cache_file = cache_file or os.path.join(os.path.dirname(__file__), '.tld_set')
        self._extractor = None
        self.tld=''
        self.domain=''
        self.subdomain=''
    def __call__(self, host):
        """
        Takes a string URL and splits it into its subdomain, domain, and
        gTLD/ccTLD component.

        >>> extract = TLDExtract()
        >>> extract('http://forums.news.cnn.com/')
        ExtractResult(subdomain='forums.news', domain='cnn', tld='com')
        >>> extract('http://forums.bbc.co.uk/')
        ExtractResult(subdomain='forums', domain='bbc', tld='co.uk')
        """
        return self._extract(host)

    def _extract(self, host):                
        registered_domain, tld = self._get_tld_extractor().extract(host)    
        subdomain, _, domain = registered_domain.rpartition('.')
        self.tld=tld
        self.subdomain=subdomain
        self.domain=domain



    def _get_tld_extractor(self):
        if self._extractor:
            return self._extractor

        cached_file = self.cache_file
        try:
            with open(cached_file) as f:
                self._extractor = _PublicSuffixListTLDExtractor(pickle.load(f))
                return self._extractor
        except IOError as ioe:
            file_not_found = ioe.errno == errno.ENOENT
            if not file_not_found:
              LOG.error("error reading TLD cache file %s: %s", cached_file, ioe)
        except Exception as ex:
            LOG.error("error reading TLD cache file %s: %s", cached_file, ex)

        tlds = frozenset()
        if self.fetch:
            tld_sources = (_PublicSuffixListSource,)
            tlds = frozenset(tld for tld_source in tld_sources for tld in tld_source())

        if not tlds:
            with pkg_resources.resource_stream(__name__, '.tld_set_snapshot') as snapshot_file:
                self._extractor = _PublicSuffixListTLDExtractor(pickle.load(snapshot_file))
                return self._extractor

        LOG.info("computed TLDs: [%s, ...]", ', '.join(list(tlds)[:10]))
        if LOG.isEnabledFor(logging.DEBUG):
            import difflib
            with pkg_resources.resource_stream(__name__, '.tld_set_snapshot') as snapshot_file:
                snapshot = sorted(pickle.load(snapshot_file))
            new = sorted(tlds)
            for line in difflib.unified_diff(snapshot, new, fromfile=".tld_set_snapshot", tofile=cached_file):
                print(line.encode('utf-8'), file=sys.stderr)

        try:
            with open(cached_file, 'wb') as f:
                pickle.dump(tlds, f)
        except IOError as e:
            LOG.warn("unable to cache TLDs in file %s: %s", cached_file, e)

        self._extractor = _PublicSuffixListTLDExtractor(tlds)
        return self._extractor

def _fetch_page(url):
    try:
        return str(urllib.request.urlopen(url).read(), 'utf-8')
    except urllib.error.URLError as e:
        LOG.error(e)
        return ''

def _PublicSuffixListSource():
    page = _fetch_page('http://mxr.mozilla.org/mozilla-central/source/netwerk/dns/effective_tld_names.dat?raw=1')

    tld_finder = re.compile(r'^(?P<tld>[.*!]*\w[\S]*)', re.UNICODE | re.MULTILINE)
    tlds = [m.group('tld') for m in tld_finder.finditer(page)]
    return tlds

class _PublicSuffixListTLDExtractor(object):
    def __init__(self, tlds):
        self.tlds = tlds

    def extract(self, netloc):
        spl = bytes(netloc).split('.')
        for i in range(len(spl)):
            maybe_tld = b'.'.join(spl[i:])
            exception_tld = b'!' + maybe_tld
            if exception_tld in self.tlds:
                return b'.'.join(spl[:i+1]), '.'.join(spl[i+1:])

            wildcard_tld = b'*.' + b'.'.join(spl[i+1:])
            if wildcard_tld in self.tlds or maybe_tld in self.tlds:
                return b'.'.join(spl[:i]), maybe_tld

        return netloc, b''