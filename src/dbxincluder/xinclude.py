#
# Copyright (c) 2016 SUSE Linux GmbH
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301 USA
#

"""xinclude module: Processes raw XInclude 1.1 elements"""

import os.path
import sys
import urllib.request

from lxml.etree import fromstring, QName
from .utils import DBXIException, NS, QN


class ResourceError(DBXIException):
    """Same as DBXIException, just for resource errors"""


def append_to_text(elem, string):
    """Append str to elem's text."""
    if elem.text:
        elem.text += string
    else:
        elem.text = string


def append_to_tail(elem, string):
    """Append str to elem's tail."""
    if elem.tail:
        elem.tail += string
    else:
        elem.tail = string


def copy_attributes(elem, subtree):
    """Modifies subtree according to
    https://www.w3.org/XML/2012/08/xinclude-11/Overview.html#attribute-copying
    with the attributes of elem. Does not return anything.

    :param elem: XInclude source elemend
    :param subtree: Target subtree/element"""

    # Iterate all attributes
    for name, value in elem.items():
        qname = QName(name)
        if qname.namespace is None and qname.localname == "set-xml-id":
            # Override/Remove xml:id on all top-level elements
            if value:
                subtree.set(QN['xml:id'], value)
            elif subtree.get(QN['xml:id']):
                del subtree.attrib[QN['xml:id']]
        elif qname.namespace == NS['local']:
            # Set attribute on all top-level elements
            subtree.set(qname.localname, value)
        elif qname.namespace == NS['xml']:
            # Ignore xml: namespace
            continue
        elif qname.namespace is not None:
            # Set attribute on all top-level elements
            subtree.set(name, value)


def get_target(elem, base_url, file=None):
    """Return tuple of the content of the target document as string and the URL that was used

    :param elem: XInclude element
    :param base_url: xml:base of the element
    :raises DBXIException: href attribute is missing
    :raises ResourceError: Couldn't fetch target
    """

    # Get href
    href = elem.get("href")
    if href is None:
        raise DBXIException(elem, "Missing href attribute", file)

    # Build full URL
    url = "/".join(base_url.split("/")[:-1]) + "/" + href

    try:
        if "://" in base_url:
            target = urllib.request.urlopen(base_url)
        else:  # Add file:// for URLs without scheme
            target = urllib.request.urlopen("file://" + os.path.abspath(url))
        content = target.read()
        target.close()
    except urllib.error.URLError:
        raise ResourceError(elem, "Could not get target {0!r}".format(url), file)
    except IOError as ioex:
        raise ResourceError(elem, "Could not get target {0!r}: {1}".format(url, ioex), file)

    return content, url


def handle_xifallback(elem, file=None, xinclude_stack=None):
    """Process the xi:include tag elem.
    It will be replaced by the content of the xi:fallback subelement.

    :param elem: The XInclude element to process
    :param file: URL used to report errors
    :param xinclude_stack: List (or None) of str with url and fragid to detect infinite recursion
    :return: True if xi:fallback found"""

    # There can be only xi:fallback in a xi:include, so just use the first child
    if len(elem) == 0 or not isinstance(elem.tag, str) or QName(elem[0]) != QN['xi:fallback']:
        return False

    # Save the tailing text
    append_to_tail(elem[0], elem.tail)

    # process_tree before replacement to not lose xml:base on xi:include or xi:fallback
    process_tree(elem[0], None, file, xinclude_stack)

    # Two passes for fallback processing, flatten them after process_tree
    elem.getparent().replace(elem, elem[0])
    return True


def validate_xinclude(elem, file):
    """Raise DBXIException if the XInclude element elem is not valid."""

    valid_attributes = ["href", "fragid", "parse", "set-xml-id"]

    for attr in elem.keys():
        qname = QName(attr)
        if qname.namespace is None and qname.localname not in valid_attributes:
            raise DBXIException(elem, "Invalid attribute {0!r}".format(str(qname)), file)

    parse = elem.get("parse", "xml")
    if parse not in ["xml", "text"]:
        raise DBXIException(elem, "Invalid value for parse: {0!r}".format(parse))

    fragid = elem.get("fragid")
    if parse != "xml" and fragid is not None:
        raise DBXIException(elem, "fragid invalid, parse != 'xml'", file)

    if len(elem) != 0 and (len(elem) > 1 or QName(elem[0]) != QN['xi:fallback']):
        raise DBXIException(elem, "Only one xi:fallback can be a child of xi:include", file)


def handle_xinclude(elem, base_url, file=None, xinclude_stack=None):
    """Process the xi:include tag elem.

    :param elem: The XInclude element to process
    :param base_url: xml:base to use if not specified in the document
    :param file: URL used to report errors
    :param xinclude_stack: List (or None) of str with url and fragid to detect infinite recursion"""

    assert QName(elem) == QN["xi:include"], "Not an XInclude"
    assert elem.getparent() is not None, "XInclude without parent"

    if elem.get("xpointer") is not None:
        assert False, "xpointer not implemented. Use fragid instead"

    # Validate attributes
    validate_xinclude(elem, file)

    # Get base (nearest xml:base or current directory)
    base_urls = elem.xpath("ancestor-or-self::*[@xml:base][1]/@xml:base")

    base_url = base_urls[0] if len(base_urls) == 1 else base_url
    if base_url is None:
        raise DBXIException(elem, "Could not get base URL", file)

    # Load target
    try:
        content, url = get_target(elem, base_url, file)
    except ResourceError as rex:
        if not handle_xifallback(elem, file, xinclude_stack):
            raise rex

        # Is this output appropriate?
        sys.stderr.write(str(rex) + "\n")
        return

    # Save text after element
    saved_tail = elem.tail
    elem.tail = ""

    # Include as text
    if elem.get("parse", "xml") != "xml":
        prev = elem.getprevious()
        if prev is not None:
            prev.tail += str(content, encoding="utf-8") + saved_tail
        else:
            elem.getparent().text += str(content, encoding="utf-8") + saved_tail
        return

    # Check for infinite recursion
    if xinclude_stack is None:
        xinclude_stack = []

    fragid = elem.get("fragid")
    xinclude_id = "{0!r}>{1!r}".format(url, fragid)
    if xinclude_id in xinclude_stack:
        raise DBXIException(elem, "Infinite recursion detected", file)

    # Parse as XML
    subtree = fromstring(content)

    # Get subdocument
    if fragid is not None:
        subtree = subtree.xpath("//*[@xml:id={0!r}]".format(fragid))
        if len(subtree) == 1:
            subtree = subtree[0]
            # Get xml:base of subdocument
            base_urls = subtree.xpath("ancestor-or-self::*[@xml:base][1]/@xml:base")
            url = base_urls[0] if len(base_urls) == 1 else url
        else:
            raise DBXIException(elem, file=file,
                                message="Could not find fragid {0!r} in target {1!r}"
                                .format(fragid, url))

    # Copy certain attributes from xi:include to the target tree
    copy_attributes(elem, subtree)

    subtree.tail = saved_tail
    process_tree(subtree, url, url, xinclude_stack + [xinclude_id])

    # Replace XInclude by subtree
    elem.getparent().replace(elem, subtree)


def process_subtree(tree, base_url, file, xinclude_stack):
    """Like process_tree, but for subtrees."""

    # for elem in tree.getiterator() does not work here, as we modify tree in-place
    for elem in tree:
        if not isinstance(elem.tag, str):
            continue

        if QName(elem) == QN["xi:include"]:
            handle_xinclude(elem, base_url, file, xinclude_stack)
            # handle_xinclude calls process_tree itself if required
        else:
            process_subtree(elem, base_url, file, xinclude_stack)


def flatten_subtree(tree):
    """Remove all xi:fallback elements in tree by replacing them with their content."""

    i = 0
    while i < len(tree):
        elem = tree[i]
        if not isinstance(elem.tag, str):
            i += 1
            continue

        if QName(elem) == QN['xi:fallback']:
            # Copy tail
            if len(elem):
                append_to_tail(elem[-1], elem.tail)
            else:
                append_to_text(elem, elem.tail)

            # Copy text
            prev = elem.getprevious()
            if prev is not None:
                append_to_tail(prev, elem.text)
            else:
                append_to_text(tree, elem.text)

            # Copy child elements
            for subelem in elem:
                tree.insert(tree.index(elem), subelem)

            tree.remove(elem)
        else:
            i += 1
            flatten_subtree(elem)


def process_tree(tree, base_url=None, file=None, xinclude_stack=None):
    """Processes an ElementTree:
    - Search and process xi:include
    - Add xml:base (=source) to the root element

    :param tree: ElementTree to process (gets modified)
    :param base_url: xml:base to use if not set in the tree
    :param file: URL used to report errors
    :param xinclude_stack: Internal"""

    if base_url and not tree.get(QN['xml:base']):
        tree.set(QN['xml:base'], base_url)

    process_subtree(tree, base_url, file, xinclude_stack)
    flatten_subtree(tree)
