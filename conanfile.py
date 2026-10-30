from conan import ConanFile


class VantageLabConan(ConanFile):
    python_requires = "adas-toolchain/1.0.6"
    python_requires_extend = "adas-toolchain.AdasComponent"
