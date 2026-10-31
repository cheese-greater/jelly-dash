"""Top processes by CPU share, computed between samples like top/btop."""

import os
import pwd
import time

PAGE_SIZE = os.sysconf('SC_PAGE_SIZE')
CLOCK_TICKS = os.sysconf('SC_CLK_TCK')


class ProcessCollector:
    def __init__(self, top_count=15, show_kernel_threads=False):
        self.top_count = top_count
        self.show_kernel_threads = show_kernel_threads
        self._previous = {}
        self._previous_time = None
        self._user_cache = {}

    def _user_name(self, uid):
        if uid not in self._user_cache:
            try:
                self._user_cache[uid] = pwd.getpwuid(uid).pw_name
            except KeyError:
                self._user_cache[uid] = str(uid)
        return self._user_cache[uid]

    def collect(self):
        now = time.monotonic()
        elapsed = (now - self._previous_time) if self._previous_time else None
        cpu_count = os.cpu_count() or 1
        current = {}
        processes = []
        running = 0
        total = 0
        for entry in os.listdir('/proc'):
            if not entry.isdigit():
                continue
            pid = int(entry)
            try:
                with open(f'/proc/{pid}/stat', 'r', encoding='utf-8', errors='replace') as handle:
                    stat_line = handle.read()
                with open(f'/proc/{pid}/status', 'r', encoding='utf-8', errors='replace') as handle:
                    status_text = handle.read()
            except OSError:
                continue
            total += 1
            name_start = stat_line.find('(')
            name_end = stat_line.rfind(')')
            name = stat_line[name_start + 1:name_end]
            fields = stat_line[name_end + 2:].split()
            state = fields[0]
            if state == 'R':
                running += 1
            ticks = int(fields[11]) + int(fields[12])
            rss_bytes = int(fields[21]) * PAGE_SIZE
            current[pid] = ticks
            uid = 0
            threads = 1
            for line in status_text.splitlines():
                if line.startswith('Uid:'):
                    uid = int(line.split()[1])
                elif line.startswith('Threads:'):
                    threads = int(line.split()[1])
            is_kernel_thread = rss_bytes == 0 and self._is_kernel_thread(pid)
            if is_kernel_thread and not self.show_kernel_threads:
                continue
            cpu_percent = 0.0
            previous_ticks = self._previous.get(pid)
            if elapsed and previous_ticks is not None:
                cpu_percent = 100.0 * (ticks - previous_ticks) / CLOCK_TICKS / elapsed / cpu_count
            processes.append({
                'pid': pid,
                'name': name,
                'user': self._user_name(uid),
                'state': state,
                'threads': threads,
                'cpu': round(max(cpu_percent, 0.0), 1),
                'mem': rss_bytes
            })
        self._previous = current
        self._previous_time = now
        processes.sort(key=lambda process: (process['cpu'], process['mem']), reverse=True)
        return {'count': total, 'running': running, 'top': processes[:self.top_count]}

    @staticmethod
    def _is_kernel_thread(pid):
        try:
            with open(f'/proc/{pid}/cmdline', 'rb') as handle:
                return handle.read(1) == b''
        except OSError:
            return True
