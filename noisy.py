import argparse
import datetime
import json
import logging
import random
import re
import sys
import time
import configparser

import requests
from urllib3.exceptions import LocationParseError

try:                 # Python 2
    from urllib.parse import urljoin, urlparse
except ImportError:  # Python 3
    from urlparse import urljoin, urlparse

try:                 # Python 2
    reload(sys)
    sys.setdefaultencoding('latin-1')
except NameError:    # Python 3
    pass


class Crawler(object):
    def __init__(self):
        """
        Initializes the Crawl class
        """
        self.config = None
        self.root_urls = []
        self.blacklisted_urls = []
        self.user_agents = []
        self._links = []
        self._start_time = None

    class CrawlerTimedOut(Exception):
        """
        Raised when the specified timeout is exceeded
        """
        pass

    def _request(self, url):
        """
        Sends a POST/GET requests using a random user agent
        :param url: the url to visit
        :return: the response Requests object
        """
        random_user_agent = random.choice(self.user_agents)
        headers = {'user-agent': random_user_agent}

        response = requests.get(url, headers=headers, timeout=5)

        return response

    @staticmethod
    def _normalize_link(link, root_url):
        """
        Normalizes links extracted from the DOM by making them all absolute, so
        we can request them, for example, turns a "/images" link extracted from https://imgur.com
        to "https://imgur.com/images"
        :param link: link found in the DOM
        :param root_url: the URL the DOM was loaded from
        :return: absolute link
        """
        try:
            parsed_url = urlparse(link)
        except ValueError:
            # urlparse can get confused about urls with the ']'
            # character and thinks it must be a malformed IPv6 URL
            return None
        parsed_root_url = urlparse(root_url)

        # '//' means keep the current protocol used to access this URL
        if link.startswith("//"):
            return "{}://{}{}".format(parsed_root_url.scheme, parsed_url.netloc, parsed_url.path)

        # possibly a relative path
        if not parsed_url.scheme:
            return urljoin(root_url, link)

        return link

    @staticmethod
    def _is_valid_url(url):
        """
        Check if a url is a valid url.
        Used to filter out invalid values that were found in the "href" attribute,
        for example "javascript:void(0)"
        taken from https://stackoverflow.com/questions/7160737
        :param url: url to be checked
        :return: boolean indicating whether the URL is valid or not
        """
        regex = re.compile(
            r'^(?:http|ftp)s?://'  # http:// or https://
            r'(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+(?:[A-Z]{2,6}\.?|[A-Z0-9-]{2,}\.?)|'  # domain...
            r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})'  # ...or ip
            r'(?::\d+)?'  # optional port
            r'(?:/?|[/?]\S+)$', re.IGNORECASE)
        return re.match(regex, url) is not None

    def _is_blacklisted(self, url):
        """
        Checks is a URL is blacklisted
        :param url: full URL
        :return: boolean indicating whether a URL is blacklisted or not
        """
        return any(blacklisted_url in url for blacklisted_url in self.blacklisted_urls)

    def _should_accept_url(self, url):
        """
        filters url if it is blacklisted or not valid, we put filtering logic here
        :param url: full url to be checked
        :return: boolean of whether or not the url should be accepted and potentially visited
        """
        return url and self._is_valid_url(url) and not self._is_blacklisted(url)

    def _extract_urls(self, body, root_url):
        """
        gathers links to be visited in the future from a web page's body.
        does it by finding "href" attributes in the DOM
        :param body: the HTML body to extract links from
        :param root_url: the root URL of the given body
        :return: list of extracted links
        """
        pattern = r"href=[\"'](?!#)(.*?)[\"'].*?"  # ignore links starting with #, no point in re-visiting the same page
        urls = re.findall(pattern, str(body))

        normalize_urls = [self._normalize_link(url, root_url) for url in urls]
        filtered_urls = list(filter(self._should_accept_url, normalize_urls))

        return filtered_urls

    def _remove_and_blacklist(self, link):
        """
        Removes a link from our current links list
        and blacklists it so we don't visit it in the future
        :param link: link to remove and blacklist
        """
        self.blacklisted_urls.append(link)
        del self._links[self._links.index(link)]

    def _browse_from_links(self, depth=0):
        """
        Selects a random link out of the available link list and visits it.
        Blacklists any link that is not responsive or that contains no other links.
        Please note that this function is recursive and will keep calling itself until
        a dead end has reached or when we ran out of links
        :param depth: our current link depth
        """
        is_depth_reached = depth >= int(self.config['config']['max_depth'])
        if not len(self._links) or is_depth_reached:
            logging.debug("Hit a dead end, moving to the next root URL")
            # escape from the recursion, we don't have links to continue or we have reached the max depth
            return

        if self._is_timeout_reached():
            raise self.CrawlerTimedOut

        random_link = random.choice(self._links)
        try:
            logging.info("Visiting {}".format(random_link))
            sub_page = self._request(random_link).content
            sub_links = self._extract_urls(sub_page, random_link)

            # sleep for a random amount of time
            time.sleep(random.randrange(int(self.config['config']["min_sleep"]), int(self.config['config']["max_sleep"])))

            # make sure we have more than 1 link to pick from
            if len(sub_links) > 1:
                # extract links from the new page
                self._links = self._extract_urls(sub_page, random_link)
            else:
                # else retry with current link list
                # remove the dead-end link from our list
                self._remove_and_blacklist(random_link)

        except requests.exceptions.RequestException:
            logging.debug("Exception on URL: %s, removing from list and trying again!" % random_link)
            self._remove_and_blacklist(random_link)

        self._browse_from_links(depth + 1)

    def load_config_file(self):
        """
        Loads and decodes a JSON config file, sets the config of the crawler instance
        to the loaded one
        :return:
        """
        self.config = configparser.ConfigParser()
        self.config.read("config.ini")

    # def set_config(self, config):
    #     """
    #     Sets the config of the crawler instance to the provided dict
    #     :param config: dict of configuration options, for example:
    #     {
    #         "root_urls": [],
    #         "blacklisted_urls": [],
    #         "click_depth": 5
    #         ...
    #     }
    #     """
    #     self._config = config

    def set_option(self, option, value):
        """
        Sets a specific key in the config dict
        :param option: the option key in the config, for example: "max_depth"
        :param value: value for the option
        """
        self.config["config"][option] = str(value)

    def _is_timeout_reached(self):
        """
        Determines whether the specified timeout has reached, if no timeout
        is specified then return false
        :return: boolean indicating whether the timeout has reached
        """
        timeout = self.config["config"]["timeout"]

        if timeout.isnumeric():
            end_time = self._start_time + datetime.timedelta(seconds=int(timeout))
            return datetime.datetime.now() >= end_time
        else:
            return False  # No timeout desired


    def parse_root_urls(self):
        """
        Parse root urls in to dictionary
        :return:
        """
        with open(self.config["filepaths"]["root_urls"], encoding="UTF-8") as urls:  # путь в конфиг
            self.root_urls = [line.strip() for line in urls]


    def parse_blacklisted_urls(self):
        """
        Parse blacklisted urls in to dictionary
        :return:
        """
        with open(self.config["filepaths"]["blacklisted_urls"], encoding="UTF-8") as urls:  # путь в конфиг
            self.blacklisted_urls = [line.strip() for line in urls]


    def parse_user_agents(self):
        """
        Parse user agents in to dictionary
        :return:
        """
        with open(self.config["filepaths"]["user_agents"], encoding="UTF-8") as user_agent:  # путь в конфиг
            self.user_agents = [line.strip() for line in user_agent]


    def crawl(self):
        """
        Collects links from our root urls, stores them and then calls
        `_browse_from_links` to browse them
        """
        self._start_time = datetime.datetime.now()

        while True:
            url = random.choice(self.root_urls)
            try:
                body = self._request(f"https://{url}").content
                self._links = self._extract_urls(body, url)
                logging.debug("found {} links".format(len(self._links)))
                self._browse_from_links()

            except requests.exceptions.RequestException:
                logging.warning("Error connecting to root url: {}".format(url))
                
            except MemoryError:
                logging.warning("Error: content at url: {} is exhausting the memory".format(url))

            except LocationParseError:
                logging.warning("Error encountered during parsing of: {}".format(url))

            except self.CrawlerTimedOut:
                logging.warning("Timeout has exceeded, exiting")
                return

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--log', metavar='-l', type=str, help='logging level', default='info')
    parser.add_argument('--timeout', metavar='-t', required=False, type=int,
                        help='for how long the crawler should be running, in seconds', default=False)
    args = parser.parse_args()

    level = getattr(logging, args.log.upper())
    logging.basicConfig(level=level)

    crawler = Crawler()
    crawler.load_config_file()
    crawler.parse_blacklisted_urls()
    crawler.parse_root_urls()
    crawler.parse_user_agents()

    if args.timeout:
        crawler.set_option('timeout', args.timeout)

    crawler.crawl()


if __name__ == '__main__':
    main()
