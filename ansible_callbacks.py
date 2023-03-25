import re


def ram_amount(stdout):
    if m := re.search(r"(\d+)\s*kB", stdout):
        return f"{round(int(m.group(1)) * 10 ** -6)} GB"
    return None


def cpu_number(stdout):
    if m := re.search(r"CPU\(s\):\s*(\d+)", stdout):
        return f"{m.group(1)} CPU(s)"
    return None


def peak_ram_usage(stdout):
    if m := re.search(r"MEM\s*([\d.]+)", stdout):
        return f"{m.group(1)}%"
    return None


def peak_cpu_usage(stdout):
    if m := re.search(r"CPU\s*([\d.]+)", stdout):
        return f"{m.group(1)}%"
    return None
