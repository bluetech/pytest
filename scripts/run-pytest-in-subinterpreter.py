#!/usr/bin/env python
# Run pytest in a subinterpreter for testing purposes.
#
# WARNING: Does not supoort Ctrl-C.
#          https://github.com/python/cpython/issues/113130

import sys
from concurrent import interpreters

interp = interpreters.create()
interp.prepare_main(args=tuple(sys.argv[1:]))
interp.exec("""
import pytest
pytest.main(list(args))
""")
