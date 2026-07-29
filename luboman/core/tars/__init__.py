#! /usr/bin/env python
# -*- coding: utf-8 -*-

# 移植自 biliup (biliup/common/tars)，仅保留虎牙 wup 协议所需的最小子集
# https://github.com/biliup/biliup/tree/master/biliup/common/tars

from .__tars import TarsInputStream
from .__tars import TarsOutputStream
from .__tup import TarsUniPacket
from .__util import util


class tarscore:
    class TarsInputStream(TarsInputStream):
        pass

    class TarsOutputStream(TarsOutputStream):
        pass

    class TarsUniPacket(TarsUniPacket):
        pass

    class boolean(util.boolean):
        pass

    class int8(util.int8):
        pass

    class uint8(util.uint8):
        pass

    class int16(util.int16):
        pass

    class uint16(util.uint16):
        pass

    class int32(util.int32):
        pass

    class uint32(util.uint32):
        pass

    class int64(util.int64):
        pass

    class float(util.float):
        pass

    class double(util.double):
        pass

    class bytes(util.bytes):
        pass

    class string(util.string):
        pass

    class struct(util.struct):
        pass

    @staticmethod
    def mapclass(ktype, vtype): return util.mapclass(ktype, vtype)

    @staticmethod
    def vctclass(vtype): return util.vectorclass(vtype)

    @staticmethod
    def printHex(buff): util.printHex(buff)
