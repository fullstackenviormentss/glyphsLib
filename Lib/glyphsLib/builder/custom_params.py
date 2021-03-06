# Copyright 2015 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import (print_function, division, absolute_import,
                        unicode_literals)

from collections import defaultdict
import re

from glyphsLib.util import bin_to_int_list, int_list_to_bin
from .filters import parse_glyphs_filter, write_glyphs_filter
from .constants import (GLYPHS_PREFIX, PUBLIC_PREFIX,
                        UFO2FT_FILTERS_KEY, UFO2FT_USE_PROD_NAMES_KEY,
                        CODEPAGE_RANGES, REVERSE_CODEPAGE_RANGES)
from .features import replace_feature

"""Set Glyphs custom parameters in UFO info or lib, where appropriate.

Custom parameter data will be extracted from a Glyphs object such as GSFont,
GSFontMaster or GSInstance by wrapping it in the GlyphsObjectProxy.
This proxy normalizes and speeds up the API used to access custom parameters,
and also keeps track of which customParameters have been read from the object.

Note:
    In the special case of GSInstance -> UFO, the source object is not
    actually the GSInstance but a designspace InstanceDescriptor wrapped in
    InstanceDescriptorAsGSInstance. This is because the generation of
    instance UFOs from a Glyphs font happens in two steps:

        1. the GSFont is turned into a designspace + master UFOS
        2. the designspace + master UFOs are interpolated into instance UFOs

    We want step 2. to rely only on information from the designspace, that's why
    we use the InstanceDescriptor as a source of customParameters to put into
    the instance UFO.

In the other direction, put information from UFO info or lib into a GSFont or a
GSFontMaster. The UFO source is wrapped in a UFOProxy that records which
attributes are read/written.

In order to go in both directions, each known parameter is managed by a
ParamHandler object that can implement special rules to translate the value
between Glyphs and UFO formats. This files aims at providing at least one
handler per defined UFO info attribute, plus a bunch of handlers for known
Custom Paramerters or known UFO lib elements.

To go for example from UFO to Glyphs, each registered ParamHandler is called,
and each tries to find its parameter in the UFO's info or lib data. Accesses to
the UFO lib are recorded by the UFO proxy. After all registered ParamHandlers
have worked, we know which UFO lib fields have been "consumed" in a smart way,
and we can stupidly copy the other ones over to the Glyphs side. Same when
going from Glyphs to UFOs.
"""

CUSTOM_PARAM_PREFIX = GLYPHS_PREFIX + 'customParameter.'


def identity(value):
    return value


class GlyphsObjectProxy(object):
    """Accelerate and record access to the glyphs object's custom parameters"""
    def __init__(self, glyphs_object, glyphs_module):
        self._owner = glyphs_object
        # This is a key part to be used in UFO lib keys to be able to choose
        # between master and font attributes during roundtrip
        self.sub_key = glyphs_object.__class__.__name__ + '.'
        self._glyphs_module = glyphs_module
        self._lookup = defaultdict(list)
        for param in glyphs_object.customParameters:
            self._lookup[param.name].append(param.value)
        self._handled = set()

    def get_attribute_value(self, key):
        if not hasattr(self._owner, key):
            return None
        return getattr(self._owner, key)

    def set_attribute_value(self, key, value):
        if not hasattr(self._owner, key):
            return
        setattr(self._owner, key, value)

    def get_custom_value(self, key):
        """Return the first and only custom parameter matching the given name."""
        self._handled.add(key)
        values = self._lookup[key]
        if len(values) > 1:
            raise RuntimeError('More than one value for this customParameter: {}'.format(key))
        if values:
            return values[0]
        return None

    def get_custom_values(self, key):
        """Return a set of values for the given customParameter name."""
        self._handled.add(key)
        return self._lookup[key]

    def set_custom_value(self, key, value):
        """Set one custom parameter with the given value.
        We assume that the list of custom parameters does not already contain
        the given parameter so we only append.
        """
        self._owner.customParameters.append(
            self._glyphs_module.GSCustomParameter(name=key, value=value))

    def set_custom_values(self, key, values):
        """Set several values for the customParameter with the given key.
        We append one GSCustomParameter per value.
        """
        for value in values:
            self.set_custom_value(key, value)

    def unhandled_custom_parameters(self):
        for param in self._owner.customParameters:
            if param.name not in self._handled:
                yield param


class UFOProxy(object):
    """Record access to the UFO's lib custom parameters"""
    def __init__(self, ufo):
        self._owner = ufo
        self._handled = set()

    def has_info_attr(self, name):
        return hasattr(self._owner.info, name)

    def get_info_value(self, name):
        return getattr(self._owner.info, name)

    def set_info_value(self, name, value):
        setattr(self._owner.info, name, value)

    def has_lib_key(self, name):
        return name in self._owner.lib

    def get_lib_value(self, name):
        if name not in self._owner.lib:
            return None
        self._handled.add(name)
        return self._owner.lib[name]

    def set_lib_value(self, name, value):
        self._owner.lib[name] = value

    def unhandled_lib_items(self):
        for key, value in self._owner.lib.items():
            if (key.startswith(CUSTOM_PARAM_PREFIX) and
                    key not in self._handled):
                yield (key, value)


class AbstractParamHandler(object):
    # @abstractmethod
    def to_glyphs(self):
        pass

    # @abstractmethod
    def to_ufo(self):
        pass


class ParamHandler(AbstractParamHandler):
    def __init__(self, glyphs_name, ufo_name=None,
                 glyphs_long_name=None, glyphs_multivalued=False,
                 ufo_prefix=CUSTOM_PARAM_PREFIX, ufo_info=True,
                 ufo_default=None,
                 value_to_ufo=identity, value_to_glyphs=identity):
        self.glyphs_name = glyphs_name
        self.glyphs_long_name = glyphs_long_name
        self.glyphs_multivalued = glyphs_multivalued
        # By default, they have the same name in both
        self.ufo_name = ufo_name or glyphs_name
        self.ufo_prefix = ufo_prefix
        self.ufo_info = ufo_info
        self.ufo_default = ufo_default
        # Value transformation functions
        self.value_to_ufo = value_to_ufo
        self.value_to_glyphs = value_to_glyphs

    # By default, the parameter is read from/written to:
    #  - the Glyphs object's customParameters
    #  - the UFO's info object if it has a matching attribute, else the lib
    def to_glyphs(self, glyphs, ufo):
        ufo_value = self._read_from_ufo(glyphs, ufo)
        if ufo_value is None:
            return
        glyphs_value = self.value_to_glyphs(ufo_value)
        self._write_to_glyphs(glyphs, glyphs_value)

    def to_ufo(self, glyphs, ufo):
        glyphs_value = self._read_from_glyphs(glyphs)
        if glyphs_value is None:
            return
        ufo_value = self.value_to_ufo(glyphs_value)
        self._write_to_ufo(glyphs, ufo, ufo_value)

    def _read_from_glyphs(self, glyphs):
        # Try both the prefixed (long) name and the short name
        if self.glyphs_multivalued:
            getter = glyphs.get_custom_values
        else:
            getter = glyphs.get_custom_value
        # The value registered using the small name has precedence
        small_name_value = getter(self.glyphs_name)
        if small_name_value is not None:
            return small_name_value
        if self.glyphs_long_name is not None:
            return getter(self.glyphs_long_name)
        return None

    def _write_to_glyphs(self, glyphs, value):
        # Never write the prefixed (long) name?
        # FIXME: (jany) maybe should rather preserve the naming choice of user
        if self.glyphs_multivalued:
            glyphs.set_custom_values(self.glyphs_name, value)
        else:
            glyphs.set_custom_value(self.glyphs_name, value)

    def _read_from_ufo(self, glyphs, ufo):
        if self.ufo_info and ufo.has_info_attr(self.ufo_name):
            return ufo.get_info_value(self.ufo_name)
        else:
            ufo_prefix = self.ufo_prefix
            if ufo_prefix == CUSTOM_PARAM_PREFIX:
                ufo_prefix += glyphs.sub_key
            return ufo.get_lib_value(ufo_prefix + self.ufo_name)

    def _write_to_ufo(self, glyphs, ufo, value):
        if self.ufo_default is not None and value == self.ufo_default:
            return
        if self.ufo_info and ufo.has_info_attr(self.ufo_name):
            # most OpenType table entries go in the info object
            ufo.set_info_value(self.ufo_name, value)
        else:
            # everything else gets dumped in the lib
            ufo_prefix = self.ufo_prefix
            if ufo_prefix == CUSTOM_PARAM_PREFIX:
                ufo_prefix += glyphs.sub_key
            ufo.set_lib_value(ufo_prefix + self.ufo_name, value)


KNOWN_PARAM_HANDLERS = []


def register(handler):
    KNOWN_PARAM_HANDLERS.append(handler)


GLYPHS_UFO_CUSTOM_PARAMS = (
    ('hheaAscender', 'openTypeHheaAscender'),
    ('hheaDescender', 'openTypeHheaDescender'),
    ('hheaLineGap', 'openTypeHheaLineGap'),
    ('compatibleFullName', 'openTypeNameCompatibleFullName'),
    ('description', 'openTypeNameDescription'),
    ('license', 'openTypeNameLicense'),
    ('licenseURL', 'openTypeNameLicenseURL'),
    ('preferredFamilyName', 'openTypeNamePreferredFamilyName'),
    ('preferredSubfamilyName', 'openTypeNamePreferredSubfamilyName'),
    ('sampleText', 'openTypeNameSampleText'),
    ('WWSFamilyName', 'openTypeNameWWSFamilyName'),
    ('WWSSubfamilyName', 'openTypeNameWWSSubfamilyName'),
    ('panose', 'openTypeOS2Panose'),
    ('fsType', 'openTypeOS2Type'),
    ('typoAscender', 'openTypeOS2TypoAscender'),
    ('typoDescender', 'openTypeOS2TypoDescender'),
    ('typoLineGap', 'openTypeOS2TypoLineGap'),
    ('unicodeRanges', 'openTypeOS2UnicodeRanges'),
    ('vendorID', 'openTypeOS2VendorID'),
    # ('weightClass', 'openTypeOS2WeightClass'),
    # ('widthClass', 'openTypeOS2WidthClass'),
    # ('winAscent', 'openTypeOS2WinAscent'),
    # ('winDescent', 'openTypeOS2WinDescent'),
    ('vheaVertTypoAscender', 'openTypeVheaVertTypoAscender'),
    ('vheaVertTypoDescender', 'openTypeVheaVertTypoDescender'),
    ('vheaVertTypoLineGap', 'openTypeVheaVertTypoLineGap'),
    # Postscript parameters
    ('blueScale', 'postscriptBlueScale'),
    ('blueShift', 'postscriptBlueShift'),
    ('isFixedPitch', 'postscriptIsFixedPitch'),
    ('underlinePosition', 'postscriptUnderlinePosition'),
    ('underlineThickness', 'postscriptUnderlineThickness'),
)
for glyphs_name, ufo_name in GLYPHS_UFO_CUSTOM_PARAMS:
    register(ParamHandler(glyphs_name, ufo_name, glyphs_long_name=ufo_name))

# TODO: (jany) for all the following fields, check that they are stored in a
# meaningful Glyphs customParameter. Maybe they have short names?
GLYPHS_UFO_CUSTOM_PARAMS_NO_SHORT_NAME = (
    'openTypeHheaCaretSlopeRun',
    'openTypeVheaCaretSlopeRun',
    'openTypeHheaCaretSlopeRise',
    'openTypeVheaCaretSlopeRise',
    'openTypeHheaCaretOffset',
    'openTypeVheaCaretOffset',
    'openTypeHeadLowestRecPPEM',
    'openTypeHeadFlags',
    'openTypeNameVersion',
    'openTypeNameUniqueID',

    # TODO: (jany) look at https://forum.glyphsapp.com/t/name-table-entry-win-id4/3811/10
    # Use Name Table Entry for the next param
    'openTypeNameRecords',

    'openTypeOS2FamilyClass',
    'openTypeOS2SubscriptXSize',
    'openTypeOS2SubscriptYSize',
    'openTypeOS2SubscriptXOffset',
    'openTypeOS2SubscriptYOffset',
    'openTypeOS2SuperscriptXSize',
    'openTypeOS2SuperscriptYSize',
    'openTypeOS2SuperscriptXOffset',
    'openTypeOS2SuperscriptYOffset',
    'openTypeOS2StrikeoutSize',
    'openTypeOS2StrikeoutPosition',
    'postscriptFontName',
    'postscriptFullName',
    'postscriptSlantAngle',
    'postscriptUniqueID',

    # Should this be handled in `blue_values.py`?
    # 'postscriptFamilyBlues',
    # 'postscriptFamilyOtherBlues',
    'postscriptBlueFuzz',

    'postscriptForceBold',
    'postscriptDefaultWidthX',
    'postscriptNominalWidthX',
    'postscriptWeightName',
    'postscriptDefaultCharacter',
    'postscriptWindowsCharacterSet',

    'macintoshFONDFamilyID',
    'macintoshFONDName',

    'trademark',

    'styleMapFamilyName',
    'styleMapStyleName',
)
for name in GLYPHS_UFO_CUSTOM_PARAMS_NO_SHORT_NAME:
    register(ParamHandler(name))


# TODO: (jany) handle dynamic version number replacement
register(ParamHandler('versionString', 'openTypeNameVersion'))


class EmptyListDefaultParamHandler(ParamHandler):
    def to_glyphs(self, glyphs, ufo):
        ufo_value = self._read_from_ufo(glyphs, ufo)
        # Ingore default value == empty list
        if ufo_value is None or ufo_value == []:
            return
        glyphs_value = self.value_to_glyphs(ufo_value)
        self._write_to_glyphs(glyphs, glyphs_value)

register(EmptyListDefaultParamHandler('postscriptFamilyBlues'))
register(EmptyListDefaultParamHandler('postscriptFamilyOtherBlues'))


# convert code page numbers to OS/2 ulCodePageRange bits
register(ParamHandler(
    glyphs_name='codePageRanges',
    ufo_name='openTypeOS2CodePageRanges',
    value_to_ufo=lambda value: [CODEPAGE_RANGES[v] for v in value],
    # TODO: (jany) handle KeyError, store into userData
    value_to_glyphs=lambda value: [REVERSE_CODEPAGE_RANGES[v] for v in value if v in REVERSE_CODEPAGE_RANGES]
))
# But don't do the conversion if the Glyphs param name is written in full
register(ParamHandler(
    glyphs_name='openTypeOS2CodePageRanges',
    ufo_name='openTypeOS2CodePageRanges',
    # Don't do any conversion when writing to UFO
    # value_to_ufo=identity,
    # Don't use this handler to write back to Glyphs
    value_to_glyphs=lambda value: value # TODO: (jany) only write if contains non-codepage values
    # TODO: (jany) add test with non-codepage values
))

# enforce that winAscent/Descent are positive, according to UFO spec
for glyphs_name in ('winAscent', 'winDescent'):
    ufo_name = 'openTypeOS2W' + glyphs_name[1:]
    register(ParamHandler(
        glyphs_name, ufo_name, glyphs_long_name=ufo_name,
        value_to_ufo=abs,
        value_to_glyphs=abs,
    ))

# The value of these could be a float, and ufoLib/defcon expect an int.
for glyphs_name in ('weightClass', 'widthClass'):
    ufo_name = 'openTypeOS2W' + glyphs_name[1:]
    register(ParamHandler(glyphs_name, ufo_name, value_to_ufo=int))


# convert Glyphs' GASP Table to UFO openTypeGaspRangeRecords
def to_ufo_gasp_table(value):
    # XXX maybe the parser should cast the gasp values to int?
    value = {int(k): int(v) for k, v in value.items()}
    gasp_records = []
    # gasp range records must be sorted in ascending rangeMaxPPEM
    for max_ppem, gasp_behavior in sorted(value.items()):
        gasp_records.append({
            'rangeMaxPPEM': max_ppem,
            'rangeGaspBehavior': bin_to_int_list(gasp_behavior)})
    return gasp_records


def to_glyphs_gasp_table(value):
    return {
        str(record['rangeMaxPPEM']):
            int_list_to_bin(record['rangeGaspBehavior'])
        for record in value
    }

register(ParamHandler(
    glyphs_name='GASP Table',
    ufo_name='openTypeGaspRangeRecords',
    value_to_ufo=to_ufo_gasp_table,
    value_to_glyphs=to_glyphs_gasp_table,
))

register(ParamHandler(
    glyphs_name='Disable Last Change',
    ufo_name='disablesLastChange',
))

register(ParamHandler(
    # convert between Glyphs.app's and ufo2ft's equivalent parameter
    glyphs_name="Don't use Production Names",
    ufo_name=UFO2FT_USE_PROD_NAMES_KEY,
    ufo_prefix='',
    value_to_ufo=lambda value: not value,
    value_to_glyphs=lambda value: not value,
))


class MiscParamHandler(ParamHandler):
    """Copy GSFont attributes to ufo lib"""
    def _read_from_glyphs(self, glyphs):
        return glyphs.get_attribute_value(self.glyphs_name)

    def _write_to_glyphs(self, glyphs, value):
        glyphs.set_attribute_value(self.glyphs_name, value)


register(MiscParamHandler(glyphs_name='DisplayStrings'))
register(MiscParamHandler(glyphs_name='disablesAutomaticAlignment'))
register(MiscParamHandler(glyphs_name='iconName'))

# deal with any Glyphs naming quirks here
register(MiscParamHandler(
    glyphs_name='disablesNiceNames',
    ufo_name='useNiceNames',
    value_to_ufo=lambda value: int(not value),
    value_to_glyphs=lambda value: not bool(value)
))

for number in ('', '1', '2', '3'):
    register(MiscParamHandler('customValue' + number, ufo_info=False))
register(MiscParamHandler('weightValue', ufo_info=False))
register(MiscParamHandler('widthValue', ufo_info=False))


def append_unique(array, value):
    if value not in array:
        array.append(value)


class OS2SelectionParamHandler(AbstractParamHandler):
    flags = (
        ('Has WWS Names', 8),
        ('Use Typo Metrics', 7),
    )

    def to_glyphs(self, glyphs, ufo):
        ufo_flags = ufo.get_info_value('openTypeOS2Selection')
        if ufo_flags is None:
            return
        for glyphs_name, value in self.flags:
            if value in ufo_flags:
                glyphs.set_custom_value(glyphs_name, True)

    def to_ufo(self, glyphs, ufo):
        for glyphs_name, value in self.flags:
            if glyphs.get_custom_value(glyphs_name):
                selection = ufo.get_info_value('openTypeOS2Selection')
                if selection is None:
                    selection = []
                if value not in selection:
                    selection.append(value)
                ufo.set_info_value('openTypeOS2Selection', selection)


register(OS2SelectionParamHandler())

# Do NOT use public.glyphOrder
register(ParamHandler('glyphOrder', ufo_prefix=GLYPHS_PREFIX))


# See https://github.com/googlei18n/glyphsLib/issues/214
class FilterParamHandler(AbstractParamHandler):
    def glyphs_names(self):
        return ('Filter', 'PreFilter')

    def ufo_names(self):
        return (UFO2FT_FILTERS_KEY,)

    def to_glyphs(self, glyphs, ufo):
        ufo_filters = ufo.get_lib_value(UFO2FT_FILTERS_KEY)
        if ufo_filters is None:
            return
        for ufo_filter in ufo_filters:
            glyphs_filter, is_pre = write_glyphs_filter(ufo_filter)
            glyphs.set_custom_values('PreFilter' if is_pre else 'Filter',
                                     glyphs_filter)

    def to_ufo(self, glyphs, ufo):
        ufo_filters = []
        for pre_filter in glyphs.get_custom_values('PreFilter'):
            ufo_filters.append(parse_glyphs_filter(pre_filter, is_pre=True))
        for filter in glyphs.get_custom_values('Filter'):
            ufo_filters.append(parse_glyphs_filter(filter, is_pre=False))

        if not ufo_filters:
            return
        if not ufo.has_lib_key(UFO2FT_FILTERS_KEY):
            ufo.set_lib_value(UFO2FT_FILTERS_KEY, [])
        existing = ufo.get_lib_value(UFO2FT_FILTERS_KEY)
        existing.extend(ufo_filters)

register(FilterParamHandler())


class ReplaceFeatureParamHandler(AbstractParamHandler):
    def to_ufo(self, glyphs, ufo):
        for value in glyphs.get_custom_values('Replace Feature'):
            tag, repl = re.split("\s*;\s*", value, 1)
            ufo._owner.features.text = replace_feature(
                tag, repl, ufo._owner.features.text or "")

    def to_glyphs(self, glyphs, ufo):
        # TODO: (jany) The "Replace Feature" custom parameter can be used to
        # have one master/instance with different features than what is stored
        # in the GSFont. When going from several UFOs to one GSFont, we could
        # detect when UFOs have different features, put the common ones in
        # GSFont and replace the different ones with this custom parameter.
        # See the file `tests/builder/features_test.py`.
        pass

register(ReplaceFeatureParamHandler())


def to_ufo_custom_params(self, ufo, glyphs_object):
    # glyphs_module=None because we shouldn't instanciate any Glyphs classes
    glyphs_proxy = GlyphsObjectProxy(glyphs_object, glyphs_module=None)
    ufo_proxy = UFOProxy(ufo)

    for handler in KNOWN_PARAM_HANDLERS:
        handler.to_ufo(glyphs_proxy, ufo_proxy)

    for param in glyphs_proxy.unhandled_custom_parameters():
        name = _normalize_custom_param_name(param.name)
        ufo.lib[CUSTOM_PARAM_PREFIX + glyphs_proxy.sub_key + name] = param.value

    _set_default_params(ufo)


def to_glyphs_custom_params(self, ufo, glyphs_object):
    glyphs_proxy = GlyphsObjectProxy(glyphs_object,
                                     glyphs_module=self.glyphs_module)
    ufo_proxy = UFOProxy(ufo)

    # Handle known parameters
    for handler in KNOWN_PARAM_HANDLERS:
        handler.to_glyphs(glyphs_proxy, ufo_proxy)

    # Since all UFO `info` entries (from `fontinfo.plist`) have a registered
    # handler, the only place where we can find unexpected stuff is the `lib`.
    # See the file `tests/builder/fontinfo_test.py` for `fontinfo` coverage.
    prefix = CUSTOM_PARAM_PREFIX + glyphs_proxy.sub_key
    for name, value in ufo_proxy.unhandled_lib_items():
        name = _normalize_custom_param_name(name)
        if not name.startswith(prefix):
            continue
        name = name[len(prefix):]
        glyphs_proxy.set_custom_value(name, value)

    _unset_default_params(glyphs_object)


def _normalize_custom_param_name(name):
    """Replace curved quotes with straight quotes in a custom parameter name.
    These should be the only keys with problematic (non-ascii) characters,
    since they can be user-generated.
    """

    replacements = (
        (u'\u2018', "'"), (u'\u2019', "'"), (u'\u201C', '"'), (u'\u201D', '"'))
    for orig, replacement in replacements:
        name = name.replace(orig, replacement)
    return name


DEFAULT_PARAMETERS = (
    # ufo2ft defaults to fsType Bit 2 ("Preview & Print embedding"), while
    # Glyphs.app defaults to Bit 3 ("Editable embedding")
    ('fsType', 'openTypeOS2Type', [3]),
    # Reference:
    # https://glyphsapp.com/content/1-get-started/2-manuals/1-handbook-glyphs-2-0/Glyphs-Handbook-2.3.pdf#page=200
    ('underlineThickness', 'postscriptUnderlineThickness', 50),
    ('underlinePosition', 'postscriptUnderlinePosition', -100)
)


def _set_default_params(ufo):
    """ Set Glyphs.app's default parameters when different from ufo2ft ones.
    """
    for _, ufo_name, default_value in DEFAULT_PARAMETERS:
        if getattr(ufo.info, ufo_name) is None:
            if isinstance(default_value, list):
                # Prevent problem if the same default value list is put in
                # several unrelated objects.
                default_value = default_value[:]
            setattr(ufo.info, ufo_name, default_value)


def _unset_default_params(glyphs):
    """ Unset Glyphs.app's parameters that have default values.
    FIXME: (jany) maybe this should be taken care of in the writer? and/or
        classes should have better default values?
    """
    for glyphs_name, ufo_name, default_value in DEFAULT_PARAMETERS:
        if (glyphs_name in glyphs.customParameters and
                glyphs.customParameters[glyphs_name] == default_value):
            del(glyphs.customParameters[glyphs_name])
        # These parameters can be referred to with the two names in Glyphs
        if (glyphs_name in glyphs.customParameters and
                glyphs.customParameters[glyphs_name] == default_value):
            del(glyphs.customParameters[glyphs_name])
