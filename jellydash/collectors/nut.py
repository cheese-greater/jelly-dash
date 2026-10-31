"""Network UPS Tools: query upsd over its plain TCP protocol (no upsc needed)."""

import socket

STATUS_WORDS = {
    'OL': 'online',
    'OB': 'on battery',
    'LB': 'low battery',
    'HB': 'high battery',
    'RB': 'replace battery',
    'CHRG': 'charging',
    'DISCHRG': 'discharging',
    'BYPASS': 'bypass',
    'CAL': 'calibrating',
    'OFF': 'output off',
    'OVER': 'overload',
    'TRIM': 'trimming voltage',
    'BOOST': 'boosting voltage',
    'FSD': 'forced shutdown',
    'ALARM': 'alarm'
}


def _read_list(reader, prefix, end_marker):
    rows = []
    for raw_line in reader:
        line = raw_line.rstrip('\n')
        if line.startswith('ERR'):
            raise OSError(line)
        if line.startswith(end_marker):
            return rows
        if line.startswith(prefix):
            rows.append(line)
    raise OSError('connection closed before the list ended')


def _unquote(value):
    value = value.strip()
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    return value.replace('\\"', '"').replace('\\\\', '\\')


def nut_list_vars(host='localhost', port=3493, ups_name='', timeout=3.0, username='', password=''):
    """Return (ups_name, {variable: value}) for the named or first UPS on upsd.

    Read-only lists need no login; username/password are only sent when configured.
    """
    with socket.create_connection((host, port), timeout=timeout) as connection:
        reader = connection.makefile('r', encoding='utf-8', errors='replace', newline='\n')

        def send(command):
            connection.sendall((command + '\n').encode('utf-8'))

        def expect_ok(command, label):
            send(command)
            reply = reader.readline().strip()
            if not reply.startswith('OK'):
                raise OSError(f'{label} rejected: {reply or "no reply"}')

        if username:
            expect_ok(f'USERNAME {username}', 'username')
            expect_ok(f'PASSWORD {password}', 'password')
        if not ups_name:
            send('LIST UPS')
            names = [row.split(' ', 2)[1] for row in _read_list(reader, 'UPS ', 'END LIST UPS') if row.count(' ') >= 2]
            if not names:
                raise OSError('upsd lists no UPS')
            ups_name = names[0]
        send(f'LIST VAR {ups_name}')
        variables = {}
        for row in _read_list(reader, 'VAR ', f'END LIST VAR {ups_name}'):
            parts = row.split(' ', 3)
            if len(parts) == 4:
                variables[parts[2]] = _unquote(parts[3])
        try:
            send('LOGOUT')
        except OSError:
            pass
    return ups_name, variables


def _number(variables, key):
    try:
        return float(variables[key])
    except (KeyError, ValueError, TypeError):
        return None


def nut_status(host='localhost', port=3493, ups_name='', username='', password=''):
    name, variables = nut_list_vars(host, port, ups_name, username=username, password=password)
    tokens = (variables.get('ups.status') or '').split()
    on_battery = None
    if 'OB' in tokens:
        on_battery = True
    elif 'OL' in tokens:
        on_battery = False
    charge = _number(variables, 'battery.charge')
    load = _number(variables, 'ups.load')
    nominal_power = _number(variables, 'ups.realpower.nominal') or _number(variables, 'ups.power.nominal')
    real_power = _number(variables, 'ups.realpower')
    if real_power is None and load is not None and nominal_power:
        real_power = round(load / 100.0 * nominal_power, 1)
    if on_battery:
        status = 'low battery' if 'LB' in tokens else 'discharging'
    elif 'CHRG' in tokens:
        status = 'charging'
    elif on_battery is False:
        status = 'full' if charge is not None and charge >= 100 else 'online'
    else:
        status = 'unknown'
    warnings = []
    for token, text in (('LB', 'LOW BATTERY'), ('RB', 'replace battery'), ('OVER', 'overload'),
                        ('FSD', 'forced shutdown'), ('ALARM', 'alarm'), ('BYPASS', 'on bypass')):
        if token in tokens:
            warnings.append(text)
    test_result = variables.get('ups.test.result')
    if test_result and 'fail' in test_result.lower():
        warnings.append(f'self-test: {test_result}')
    manufacturer = variables.get('ups.mfr') or variables.get('device.mfr') or ''
    model = variables.get('ups.model') or variables.get('device.model') or ''
    return {
        'available': True,
        'source': 'nut',
        'chip': f'{manufacturer} {model}'.strip() or name,
        'ups_name': name,
        'on_battery': on_battery,
        'status': status,
        'status_text': ', '.join(STATUS_WORDS.get(token, token) for token in tokens) or 'unknown',
        'flags': tokens,
        'percent': charge,
        'voltage': _number(variables, 'battery.voltage'),
        'voltage_nominal': _number(variables, 'battery.voltage.nominal'),
        'current': None,
        'power': real_power,
        'load_pct': load,
        'nominal_power_w': nominal_power,
        'time_to_empty_s': _number(variables, 'battery.runtime'),
        'runtime_low_s': _number(variables, 'battery.runtime.low'),
        'charge_low_pct': _number(variables, 'battery.charge.low'),
        'input_voltage': _number(variables, 'input.voltage'),
        'input_voltage_nominal': _number(variables, 'input.voltage.nominal'),
        'output_voltage': _number(variables, 'output.voltage'),
        'transfer_low': _number(variables, 'input.transfer.low'),
        'transfer_high': _number(variables, 'input.transfer.high'),
        'beeper': variables.get('ups.beeper.status'),
        'test_result': test_result,
        'driver': variables.get('driver.name'),
        'warnings': warnings,
        'detail': f'nut {name}@{host}'
    }
