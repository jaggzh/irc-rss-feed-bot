import dataclasses
import logging
import re
import sys
import time
from typing import Callable, Dict, List, Optional, Tuple, Union

import bitlyshortener
import cachetools.func
from descriptors import cachedproperty
import feedparser
import requests

from . import config
from .db import Database
from .util.humanize import humanize_len

log = logging.getLogger(__name__)


@dataclasses.dataclass(unsafe_hash=True)
class FeedEntry:
    title: str = dataclasses.field(compare=False)
    long_url: str = dataclasses.field(compare=True)

    @property
    def post_url(self) -> str:
        return self.long_url

    def is_listed(self, searchlist: Dict[str, List]) -> Optional[Tuple[str, re.Match]]:  # type: ignore
        for searchlist_key, val in {'title': self.title, 'url': self.long_url}.items():
            for searchlisted_pattern in searchlist.get(searchlist_key, []):
                match = re.search(searchlisted_pattern, val)
                if match:
                    log.debug('Feed entry %s matches %s pattern %s.', self, searchlist_key, searchlisted_pattern)
                    return searchlist_key, match
        return None  # This is redundant but it prevents the mypy message " Missing return statement".


@dataclasses.dataclass
class ShortenedFeedEntry(FeedEntry):
    short_url: str = dataclasses.field(compare=False)

    @property
    def post_url(self) -> str:
        return self.short_url


@dataclasses.dataclass
class Feed:
    channel: str
    name: str
    url: str = dataclasses.field(repr=False)
    db: Database = dataclasses.field(repr=False)
    url_shortener: bitlyshortener.Shortener = dataclasses.field(repr=False)

    def __post_init__(self):
        log.debug('Initializing instance of %s.', self)
        self._feed_config = config.INSTANCE['feeds'][self.channel][self.name]
        self.entries = self._entries()  # Entries are effectively cached at this point in time.
        log.debug('Initialized instance of %s.', self)

    def __str__(self):
        return f'feed {self.name} of {self.channel}'

    @staticmethod
    def _dedupe_entries(entries: List[FeedEntry], *, after_what: str, for_what: str) -> List[FeedEntry]:
        # Remove duplicate entries while preserving order, e.g. for https://projecteuclid.org/feeds/euclid.ba_rss.xml
        entries_deduped = list(dict.fromkeys(entries))
        num_removed = len(entries) - len(entries_deduped)
        if num_removed > 0:
            log.info('After %s, removed %s duplicate entries out of %s, leaving %s, for %s.',
                     after_what, num_removed, len(entries), len(entries_deduped), for_what)
            return entries_deduped
        return entries

    def _entries(self) -> List[FeedEntry]:
        feed_config = self._feed_config

        # Retrieve entries for URL
        log.debug('Entries cache usage is %s', self._url_entries.cache_info())
        entries = self._url_entries(self.url)
        # Note: A cache is useful if the same URL is to be read for multiple feeds, sometimes for multiple channels.
        log.debug('Entries cache usage is %s', self._url_entries.cache_info())

        # Keep only whitelisted entries
        whitelist = feed_config.get('whitelist', {})
        if whitelist:
            log.debug('Filtering %s entries using whitelist for %s.', len(entries), self)
            explain = whitelist.get('explain')
            entries = [entry for entry in entries if entry.is_listed(whitelist)]
            whitelisted_entries: List[FeedEntry] = []
            for entry in entries:
                is_listed = entry.is_listed(whitelist)
                if is_listed:
                    key, match = is_listed
                    if explain and (key == 'title'):
                        span0, span1 = match.span()
                        title = entry.title
                        entry.title = title[:span0] + '*' + title[span0:span1] + '*' + title[span1:]
                    whitelisted_entries.append(entry)
            entries = whitelisted_entries
            log.debug('Filtered to %s entries using whitelist for %s.', len(entries), self)

        # Remove blacklisted entries
        blacklist = feed_config.get('blacklist', {})
        if blacklist:
            log.debug('Filtering %s entries using blacklist for %s.', len(entries), self)
            entries = [entry for entry in entries if not entry.is_listed(blacklist)]
            log.debug('Filtered to %s entries using blacklist for %s.', len(entries), self)

        # Enforce HTTPS URLs
        if feed_config.get('https', False):
            log.debug('Enforcing HTTPS for URLs in %s.', self)
            for entry in entries:
                if entry.long_url.startswith('http://'):
                    entry.long_url = entry.long_url.replace('http://', 'https://', 1)
            log.debug('Enforced HTTPS for URLs in %s.', self)

        # Substitute entries
        sub = feed_config.get('sub')
        if sub:
            log.debug('Substituting entries for %s.', self)
            re_sub: Callable[[str, Optional[Dict[str, str]]], str] = \
                lambda v, r: re.sub(r['pattern'], r['repl'], v) if r else v
            entries = [FeedEntry(title=re_sub(e.title, sub.get('title')), long_url=re_sub(e.long_url, sub.get('url')))
                       for e in entries]
            log.debug('Substituted entries for %s.', self)

        # Format entries
        format_config = feed_config.get('format')
        if format_config:
            log.debug('Formatting entries for %s.', self)
            format_re = format_config.get('re', {})
            format_str = format_config['str']
            for index, entry in enumerate(entries.copy()):  # May not strictly need `copy()`.
                params = {'title': entry.title, 'url': entry.long_url}
                for key, val in params.copy().items():
                    if key in format_re:
                        match = re.search(format_re[key], val)
                        if match:
                            params.update(match.groupdict())
                entries[index] = FeedEntry(title=format_str.get('title', '{title}').format_map(params),
                                           long_url=format_str.get('url', '{url}').format_map(params))
            log.debug('Formatted entries for %s.', self)

        # Dedupe entries again
        entries = self._dedupe_entries(entries, after_what='processing feed', for_what=str(self))

        log.debug('Returning %s entries for %s.', len(entries), self)
        return entries

    @classmethod
    @cachetools.func.ttl_cache(maxsize=sys.maxsize, ttl=config.URL_CACHE_TTL)
    def _url_entries(cls, url: str) -> List[FeedEntry]:
        # This method is feed agnostic. As such, no action requiring feed_config can be done in this method.

        # Read URL
        log.debug('Resiliently retrieving content for %s.', url)
        for num_attempt in range(1, config.READ_ATTEMPTS_MAX + 1):
            try:
                response = requests.get(url, timeout=config.REQUEST_TIMEOUT,
                                        headers={'User-Agent': config.USER_AGENT})
                response.raise_for_status()
            except requests.RequestException as exc:
                log.warning('Error reading %s in attempt %s of %s: %s', url, num_attempt, config.READ_ATTEMPTS_MAX, exc)
                if num_attempt == config.READ_ATTEMPTS_MAX:
                    raise exc from None
                time.sleep(2 ** num_attempt)
            else:
                break
        content = response.content
        log.debug('Resiliently retrieved content of size %s for %s.', humanize_len(content), url)

        # Parse entries
        log.debug('Retrieving entries for %s.', url)
        entries = [FeedEntry(title=e['title'], long_url=e['link']) for e in feedparser.parse(content)['entries']]
        logger = log.debug if entries else log.warning
        logger('Retrieved %s entries for %s.', len(entries), url)

        # Dedupe entries
        entries = cls._dedupe_entries(entries, after_what='reading feed', for_what=f'feed URL {url}')

        log.debug('Returning %s entries for %s.', len(entries), url)
        return entries

    @cachedproperty
    def postable_entries(self) -> List[Union[FeedEntry, ShortenedFeedEntry]]:
        log.debug('Retrieving postable entries for %s.', self)
        entries = self.unposted_entries

        # Filter entries if new feed
        if self.db.is_new_feed(self.channel, self.name):
            log.debug('Filtering new feed %s having %s postable entries.', self, len(entries))
            max_posts = self._feed_config.get('new', config.NEW_FEED_POSTS_DEFAULT)
            max_posts = config.NEW_FEED_POSTS_MAX[max_posts]
            entries = entries[:max_posts]
            log.debug('Filtered new feed %s to %s postable entries given a max limit of %s entries.',
                      self, len(entries), max_posts)

        # Shorten URLs
        if entries and self._feed_config.get('shorten', True):
            log.debug('Shortening %s postable long URLs for %s.', len(entries), self)
            long_urls = [entry.long_url for entry in entries]
            short_urls = self.url_shortener.shorten_urls(long_urls)
            entries = [ShortenedFeedEntry(e.title, e.long_url, short_urls[i]) for i, e in enumerate(entries)]
            log.debug('Shortened %s postable long URLs for %s.', len(entries), self)

        log.debug('Returning %s postable entries for %s.', len(entries), self)
        return entries

    @cachedproperty
    def unposted_entries(self) -> List[FeedEntry]:
        log.debug('Retrieving unposted entries for %s.', self)
        entries = self.entries
        long_urls = [entry.long_url for entry in entries]
        dedup_strategy = self._feed_config.get('dedup', config.DEDUP_STRATEGY_DEFAULT)
        if dedup_strategy == 'channel':
            long_urls = self.db.select_unposted_for_channel(self.channel, self.name, long_urls)
        else:
            assert dedup_strategy == 'feed'
            long_urls = self.db.select_unposted_for_channel_feed(self.channel, self.name, long_urls)
        long_urls = set(long_urls)
        entries = [entry for entry in entries if entry.long_url in long_urls]
        log.debug('Returning %s unposted entries for %s.', len(entries), self)
        return entries
