def bad_attr():
    str.does_not_exist

def sub_multi_fail():
    exceptions = []

    try:
        bad_attr()
    except Exception as e:
        exceptions.append(e)

    try:
        bad_attr()
    except Exception as e:
        exceptions.append(e)

    raise ExceptionGroup("sub_multi_fail failed", exceptions)


def div_by_zero():
    1 / 0

def multi_fail():
    exceptions = []

    try:
        sub_multi_fail()
    except Exception as e:
        exceptions.append(e)

    try:
        div_by_zero()
    except Exception as e:
        exceptions.append(e)

    raise ExceptionGroup("multi_fail failed", exceptions)

def func():
    multi_fail()


def test():
    func()
