import os
import re
import mmap
import json
import struct
import pkg_resources

# ---- Größen-/Typ-Tabellen ----------------------------------------------------
SIZES = {
    'Int32': 4, 'Int16': 2, 'Int8': 1,
    'Float32': 4, 'Float64': 8,
    'Double': 8, 'Single': 4,
    'UInt32': 4, 'UInt16': 2, 'UInt8': 1,
    'Int64': 8, 'UInt64': 8,
    'Boolean': 1, 'Byte': 1, 'SByte': 1,
    'Char': 2, 'String': 4, 'Void': 0,
    'byte': 1, 'sbyte': 1, 'char': 2, 'string': 4, 'void': 0,
}

# struct.pack/unpack Codes (Achtung: Float32/Float64 müssen 'f'/'d' sein!)
STRUCT_TYPES = {
    'Single': 'f', 'Double': 'd',
    'Int32': 'i', 'Int16': 'h', 'Int8': 'b',
    'UInt32': 'I', 'UInt16': 'H', 'UInt8': 'B',
    'Int64': 'q', 'UInt64': 'Q',
    'Boolean': '?',
    'Byte': 'B', 'SByte': 'b',
    'Char': 'c',
    'String': 's',
    'Void': 'x',
    'Float32': 'f', 'Float64': 'd',
    'byte': 'B', 'sbyte': 'b', 'char': 'c', 'string': 's', 'void': 'x',
}

# ---- Quelle der data.cs festlegen -------------------------------------------
ENV_DATA_CS = os.getenv("R3E_DATA_CS")
if ENV_DATA_CS and os.path.isfile(ENV_DATA_CS):
    DATA_FILE = ENV_DATA_CS
else:
    DATA_FILE = pkg_resources.resource_filename('r3e_api', 'data/data.cs')

# ---- Parser: C#-Struct-Layout einlesen --------------------------------------
def replace_if_equals(string, old, new):
    return new if string == old else string

def get_struct_string(positions):
    res = ''
    val_type = positions['type']
    if 'children' in positions:
        if isinstance(positions['children'], list):
            val_type = val_type.split('[')[0]
            if val_type in STRUCT_TYPES:
                res += str(len(positions['children'])) + STRUCT_TYPES[val_type]
            else:
                for ch in positions['children']:
                    res += get_struct_string(ch)
        else:
            for position in positions['children']:
                res += get_struct_string(positions['children'][position])
    elif positions['type'] in STRUCT_TYPES:
        res += STRUCT_TYPES[positions['type']]
    return res

def read_data_from_struct(data, positions):
    struct_string = '<' + get_struct_string(positions)
    start, end = positions['start'], positions['end']
    return unflatten_struct_data(struct.unpack(struct_string, data[start:end]), positions)

def get_child_amount(position):
    if 'children' in position:
        if isinstance(position['children'], list):
            return sum(get_child_amount(ch) for ch in position['children'])
        else:
            return sum(get_child_amount(position['children'][k]) for k in position['children'])
    else:
        return (position['end'] - position['start']) // SIZES[position['type']]

def _bytes_to_utf8(byte_seq):
    """byte[] (als Liste/Tuple) → UTF-8-String (Null-terminiert)."""
    if isinstance(byte_seq, str):
        return byte_seq
    try:
        b = bytes(int(x) & 0xFF for x in byte_seq)
        z = b.find(b'\x00')
        if z != -1:
            b = b[:z]
        return b.decode('utf-8', errors='ignore')
    except Exception:
        return ""

def unflatten_struct_data(data, positions):
    if 'children' in positions:
        if isinstance(positions['children'], list):
            out = []
            i = 0
            for ch in positions['children']:
                size = get_child_amount(ch)
                out.append(unflatten_struct_data(data[i:i+size], ch))
                i += size
            # Byte-Arrays als UTF-8-String zurückgeben
            if positions['type'].startswith('byte'):
                out = _bytes_to_utf8(out)
            return out
        else:
            out = {}
            i = 0
            for name, ch in sorted(positions['children'].items(), key=lambda x: x[1]['start']):
                size = get_child_amount(ch)
                out[name] = unflatten_struct_data(data[i:i+size], ch)
                i += size
            return out
    else:
        return data[0] if (isinstance(data, (list, tuple)) and len(data) == 1) else data

def read_struct_positions(data_lines, struct_name, generic_type=None, start=0):
    """
    Liest die Feld-Offets eines Structs aus C#-Code und gibt einen Positionsbaum zurück:
    {
        'start': int, 'end': int, 'type': str,
        'children': { name -> child }  oder  Liste bei Arrays
    }
    """
    if struct_name in STRUCT_TYPES:
        return {'start': start, 'end': SIZES[struct_name] + start, 'type': struct_name}

    struct = -1
    generic_var_name = None
    for i, line in enumerate(data_lines):
        if line.startswith('internal struct ' + struct_name):
            struct = i
            if '<' in line:
                generic_var_name = line.split('<')[1].split('>')[0]
            break
    if struct == -1:
        return None

    children = {}
    res = {'start': start, 'end': 0, 'type': struct_name, 'children': children}
    before = start

    for i in range(struct + 1, len(data_lines)):
        line = data_lines[i]

        if line.startswith('public'):
            line_type = line.split(' ')[1]
            line_name = line.split(' ')[2].split(';')[0]
            sub_type = None

            if '<' in line_type:
                line_type, sub_type = line_type.split('<')
                sub_type = sub_type.split('>')[0]
                if generic_var_name:
                    line_type = replace_if_equals(line_type, generic_var_name, generic_type)
                    sub_type = replace_if_equals(sub_type, generic_var_name, generic_type)
            elif generic_var_name:
                line_type = replace_if_equals(line_type, generic_var_name, generic_type)

            # Fester Array-Block: [MarshalAs(UnmanagedType.ByValArray, SizeConst = N)]
            if '[' in line_type:
                line_type = line_type.split('[')[0]

                if i == 0:
                    raise Exception(f'Error identifying array length of field {line_name} in struct {struct_name}')

                # Robuste Erkennung (Whitespace egal)
                prev_line = data_lines[i - 1].strip()
                if '[MarshalAs(UnmanagedType.ByValArray' not in prev_line:
                    raise Exception(f'Error identifying array length of field {line_name} in struct {struct_name}')
                m = re.search(r'SizeConst\s*=\s*(\d+)', prev_line)
                if not m:
                    raise Exception(f'Array length not found: field {line_name} in struct {struct_name}')
                length = int(m.group(1))

                children[line_name] = {'start': before, 'end': 0, 'type': line_type + '[]', 'children': []}
                obj = read_struct_positions(data_lines, line_type, sub_type, before)
                elem_size = obj['end'] - obj['start']
                children[line_name]['end'] = before + elem_size * length

                for _ in range(length):
                    children[line_name]['children'].append(obj)
                    obj = obj.copy()
                    obj['start'] += elem_size
                    obj['end'] += elem_size
                    before += elem_size
                res['end'] = children[line_name]['end']
                continue

            # Normales Feld / Sub-Struct
            children[line_name] = read_struct_positions(data_lines, line_type, sub_type, before)
            before += children[line_name]['end'] - children[line_name]['start']
            res['end'] = children[line_name]['end']

        elif line.startswith('}'):
            break
    return res

def convert(infile, outfile=None):
    with open(infile, 'r', encoding='utf-8', errors='ignore') as f:
        data = f.read()
    # Whitespace konsolidieren (Tabs entfernen ist ok, Array-Erkennung nutzt .strip())
    data = re.sub(r'\n\s+', '\n', data).replace('\t', '').replace('\r', '').split('\n')
    res = read_struct_positions(data, 'Shared')
    if outfile:
        json.dump(res, open(outfile, 'w', encoding='utf-8'), indent=4, ensure_ascii=False)
    return res

def get_value(data, field):
    positions = convert(DATA_FILE)
    for field_name in field.split('.'):
        if field_name not in positions['children']:
            try:
                field_name = int(field_name)  # Array-Index
            except Exception:
                raise Exception('Field ' + field_name + ' not found in struct ' + positions['type'])
        positions = positions['children'][field_name]
    return read_data_from_struct(data, positions)

# ---- Shared Memory Reader ----------------------------------------------------
class R3ESharedMemory:
    def __init__(self):
        self._mmap_data = None
        self._converted_data = None

    def update_offsets(self):
        self._converted_data = convert(DATA_FILE)

    @property
    def mmap_data(self):
        if not self._mmap_data:
            self.update_buffer()
        return self._mmap_data

    @property
    def converted_data(self):
        if self._converted_data is None:
            self.update_offsets()
        return self._converted_data

    def update_buffer(self):
        """
        Öffnet die benannte Map mit der *exakten* Länge aus data.cs (Shared.end).
        Vermeidet WinError 87 (falscher Parameter) und Puffer-Mismatches.
        """
        if self._converted_data is None:
            self.update_offsets()
        need = self._converted_data['end']

        last_exc = None
        for tag in ("Local\\$R3E", "$R3E"):
            try:
                mm = mmap.mmap(-1, need, tag, access=mmap.ACCESS_READ)
                try:
                    mm.seek(0)
                    buf = mm.read(need)
                finally:
                    mm.close()
                if len(buf) < need:
                    raise RuntimeError(
                        f"Shared Memory {tag} lieferte {len(buf)} Bytes, erwartet {need} Bytes – falsche data.cs?"
                    )
                self._mmap_data = buf
                return self._mmap_data
            except Exception as e:
                last_exc = e
                continue
        raise RuntimeError(f"Konnte $R3E nicht öffnen: {last_exc}")

    def get_value(self, field):
        return get_value(self._mmap_data, field)

if __name__ == '__main__':
    shared = R3ESharedMemory()
    shared.update_buffer()
    print(shared.get_value('DriverData.0'))
