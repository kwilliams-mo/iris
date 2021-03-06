# (C) British Crown Copyright 2013, Met Office
#
# This file is part of Iris.
#
# Iris is free software: you can redistribute it and/or modify it under
# the terms of the GNU Lesser General Public License as published by the
# Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Iris is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with Iris.  If not, see <http://www.gnu.org/licenses/>.
"""NAME file format loading functions."""

import collections
import datetime
from itertools import izip
import re
import warnings

import numpy as np

from iris.coords import AuxCoord, DimCoord
import iris.coord_systems
import iris.cube
import iris.exceptions
import iris.unit


EARTH_RADIUS = 6371229.0
NAMEIII_DATETIME_FORMAT = '%d/%m/%Y  %H:%M %Z'
NAMEII_FIELD_DATETIME_FORMAT = '%H%M%Z %d/%m/%Y'
NAMEII_TIMESERIES_DATETIME_FORMAT = '%d/%m/%Y  %H:%M:%S'


NAMECoord = collections.namedtuple('NAMECoord', ['name',
                                                 'dimension',
                                                 'values'])


def _split_name_and_units(name):
    units = None
    if "(" in name and ")" in name:
        split = name.rsplit("(", 1)
        try_units = split[1].replace(")", "").strip()
        try:
            try_units = iris.unit.Unit(try_units)
        except ValueError:
            pass
        else:
            name = split[0].strip()
            units = try_units
    return name, units


def read_header(file_handle):
    """
    Return a dictionary containing the header information extracted
    from the the provided NAME file object.

    Args:

    * file_handle (file-like object):
        A file-like object from which to read the header information.

    Returns:
        A dictionary containing the extracted header information.

    """
    header = {}
    header['NAME Version'] = file_handle.next().strip()
    for line in file_handle:
        words = line.split(':', 1)
        if len(words) != 2:
            break
        key, value = [word.strip() for word in words]
        header[key] = value

    # Cast some values into floats or integers if they match a
    # given name. Set any empty string values to None.
    for key, value in header.items():
        if value:
            if key in ['X grid origin', 'Y grid origin',
                       'X grid resolution', 'Y grid resolution']:
                header[key] = float(value)
            elif key in ['X grid size', 'Y grid size',
                         'Number of preliminary cols',
                         'Number of field cols',
                         'Number of fields',
                         'Number of series']:
                header[key] = int(value)
        else:
            header[key] = None

    return header


def _read_data_arrays(file_handle, n_arrays, shape):
    """
    Return a list of NumPy arrays containing the data extracted from
    the provided file object. The number and shape of the arrays
    must be specified.

    """
    data_arrays = [np.zeros(shape, dtype=np.float32) for
                   i in range(n_arrays)]

    # Iterate over the remaining lines which represent the data in
    # a column form.
    for line in file_handle:
        # Split the line by comma, removing the last empty column
        # caused by the trailing comma
        vals = line.split(',')[:-1]

        # Cast the x and y grid positions to integers and convert
        # them to zero based indices
        x = int(float(vals[0])) - 1
        y = int(float(vals[1])) - 1

        # Populate the data arrays (i.e. all columns but the leading 4).
        for i, data_array in enumerate(data_arrays):
            data_array[y, x] = float(vals[i + 4])

    return data_arrays


def _build_lat_lon_for_NAME_field(header):
    """
    Return regular latitude and longitude coordinates extracted from
    the provided header dictionary.

    """
    start = header['X grid origin']
    step = header['X grid resolution']
    count = header['X grid size']
    pts = start + np.arange(count, dtype=np.float64) * step
    lon = NAMECoord(name='longitude', dimension=1, values=pts)

    start = header['Y grid origin']
    step = header['Y grid resolution']
    count = header['Y grid size']
    pts = start + np.arange(count, dtype=np.float64) * step
    lat = NAMECoord(name='latitude', dimension=0, values=pts)

    return lat, lon


def _build_lat_lon_for_NAME_timeseries(column_headings):
    """
    Return regular latitude and longitude coordinates extracted from
    the provided column_headings dictionary.

    """
    pattern = re.compile(r'\-?[0-9]*\.[0-9]*')
    new_Xlocation_column_header = []
    for t in column_headings['X']:
        if 'Lat-Long' in t:
            matches = pattern.search(t)
            new_Xlocation_column_header.append(float(matches.group(0)))
        else:
            new_Xlocation_column_header.append(t)
    column_headings['X'] = new_Xlocation_column_header
    lon = NAMECoord(name='longitude', dimension=None,
                    values=column_headings['X'])

    new_Ylocation_column_header = []
    for t in column_headings['Y']:
        if 'Lat-Long' in t:
            matches = pattern.search(t)
            new_Ylocation_column_header.append(float(matches.group(0)))
        else:
            new_Ylocation_column_header.append(t)
    column_headings['Y'] = new_Ylocation_column_header
    lat = NAMECoord(name='latitude', dimension=None,
                    values=column_headings['Y'])

    return lat, lon


def _calc_integration_period(time_avgs):
    """
    Return a list of datetime.timedelta objects determined from the provided
    list of averaging/integration period column headings.

    """
    integration_periods = []
    pattern = re.compile(
        r'(\d{0,2})(day)?\s*(\d{1,2})(hr)?\s*(\d{1,2})(min)?\s*(\w*)')
    for time_str in time_avgs:
        days = 0
        hours = 0
        minutes = 0
        matches = pattern.search(time_str)
        if matches:
            if len(matches.group(1)) > 0:
                days = float(matches.group(1))
            if len(matches.group(3)) > 0:
                hours = float(matches.group(3))
            if len(matches.group(1)) > 0:
                minutes = float(matches.group(5))
        total_hours = days * 24.0 + hours + minutes / 60.0
        integration_periods.append(datetime.timedelta(hours=total_hours))
    return integration_periods


def _parse_units(units):
    """
    Return a known :class:`iris.unit.Unit` given a NAME unit

    .. note::

        * Some NAME units are not currently handled.
        * Units which are in the wrong case (case is ignored in NAME)
        * Units where the space between SI units is missing
        * Units where the characters used are non-standard (i.e. 'mc' for
          micro instead of 'u')

    Args:

    * units (string):
        NAME units.

    Returns:
        An instance of :class:`iris.unit.Unit`.

    """

    unit_mapper = {'Risks/m3': '1',    # Used for Bluetongue
                   'TCID50s/m3': '1',  # Used for Foot and Mouth
                   'TCID50/m3': '1',   # Used for Foot and Mouth
                   'N/A': '1',         # Used for CHEMET area at risk
                   'lb': 'pounds',     # pounds
                   'oz': '1',          # ounces
                   'deg': 'degree',    # angular degree
                   'oktas': '1',       # oktas
                   'deg C': 'deg_C',   # degrees Celsius
                   'FL': 'unknown'     # flight level
                   }

    units = unit_mapper.get(units, units)

    units = units.replace('Kg', 'kg')
    units = units.replace('gs', 'g s')
    units = units.replace('Bqs', 'Bq s')
    units = units.replace('mcBq', 'uBq')
    units = units.replace('mcg', 'ug')
    try:
        units = iris.unit.Unit(units)
    except ValueError:
        warnings.warn('Unknown units: {!r}'.format(units))
        units = iris.unit.Unit(None)

    return units


def _cf_height_from_name(z_coord):
    """
    Parser for the z component of field headings.

    This parse is specifically for handling the z component of NAME field
    headings, which include height above ground level, height above sea level
    and flight level etc.  This function returns an iris coordinate
    representing this field heading.

    Args:

    * z_coord (list):
        A field heading, specifically the z component.

    Returns:
        An instance of :class:`iris.coords.AuxCoord` representing the
        interpretation of the supplied field heading.

    """

    # NAMEII - integer/float support.
    # Match against height agl, asl and Pa.
    pattern = re.compile(r'^From\s*'
                         '(?P<lower_bound>[0-9]+(\.[0-9]+)?)'
                         '\s*-\s*'
                         '(?P<upper_bound>[0-9]+(\.[0-9]+)?)'
                         '\s*(?P<type>m\s*asl|m\s*agl|Pa)'
                         '(?P<extra>.*)')

    # Match against flight level.
    pattern_fl = re.compile(r'^From\s*'
                            '(?P<type>FL)'
                            '(?P<lower_bound>[0-9]+(\.[0-9]+)?)'
                            '\s*-\s*FL'
                            '(?P<upper_bound>[0-9]+(\.[0-9]+)?)'
                            '(?P<extra>.*)')

    # NAMEIII - integer/float support.
    # Match scalar against height agl, asl, Pa, FL
    pattern_scalar = re.compile(r'Z\s*=\s*'
                                '(?P<point>[0-9]+(\.[0-9]+)?)'
                                '\s*(?P<type>m\s*agl|m\s*asl|FL|Pa)'
                                '(?P<extra>.*)')

    type_name = {'magl': 'height', 'masl': 'altitude', 'FL': 'flight_level',
                 'Pa': 'air_pressure'}
    patterns = [pattern, pattern_fl, pattern_scalar]

    units = 'no-unit'
    points = z_coord
    bounds = None
    standard_name = None
    long_name = 'z'
    for pattern in patterns:
        match = pattern.match(z_coord)
        if match:
            match = match.groupdict()
            # Do not interpret if there is additional information to the match
            if match['extra']:
                break
            units = match['type'].replace(' ', '')
            name = type_name[units]

            # Interpret points if present.
            if 'point' in match:
                points = float(match['point'])
            # Interpret points from bounds.
            else:
                bounds = np.array([float(match['lower_bound']),
                                   float(match['upper_bound'])])
                points = bounds.sum() / 2.

            long_name = None
            if name in ['height', 'altitude']:
                units = units[0]
                standard_name = name
            elif name == 'air_pressure':
                standard_name = name
            elif name == 'flight_level':
                long_name = name
            units = _parse_units(units)

            break

    coord = AuxCoord(points, units=units, standard_name=standard_name,
                     long_name=long_name, bounds=bounds)

    return coord


def _generate_cubes(header, column_headings, coords, data_arrays):
    """
    Yield :class:`iris.cube.Cube` instances given
    the headers, column headings, coords and data_arrays extracted
    from a NAME file.

    """
    for i, data_array in enumerate(data_arrays):
        # Turn the dictionary of column headings with a list of header
        # information for each field into a dictionary of headings for
        # just this field.
        field_headings = {k: v[i] for k, v in
                          column_headings.iteritems()}

        # Make a cube.
        cube = iris.cube.Cube(data_array)

        # Determine the name and units.
        name = '{} {}'.format(field_headings['Species'],
                              field_headings['Quantity'])
        name = name.upper().replace(' ', '_')
        cube.rename(name)

        # Some units are not in SI units, are missing spaces or typed
        # in the wrong case. _parse_units returns units that are
        # recognised by Iris.
        cube.units = _parse_units(field_headings['Unit'])

        # Define and add the singular coordinates of the field (flight
        # level, time etc.)
        z_coord = _cf_height_from_name(field_headings['Z'])
        cube.add_aux_coord(z_coord)

        # Define the time unit and use it to serialise the datetime for
        # the time coordinate.
        time_unit = iris.unit.Unit(
            'hours since epoch', calendar=iris.unit.CALENDAR_GREGORIAN)

        # Build time, latitude and longitude coordinates.
        for coord in coords:
            pts = coord.values
            coord_sys = None
            if coord.name == 'latitude' or coord.name == 'longitude':
                coord_units = 'degrees'
                coord_sys = iris.coord_systems.GeogCS(EARTH_RADIUS)
            if coord.name == 'time':
                coord_units = time_unit
                pts = time_unit.date2num(coord.values)

            if coord.dimension is not None:
                icoord = DimCoord(points=pts,
                                  standard_name=coord.name,
                                  units=coord_units,
                                  coord_system=coord_sys)
                if coord.name == 'time' and 'Av or Int period' in \
                        field_headings:
                    dt = coord.values - \
                        field_headings['Av or Int period']
                    bnds = time_unit.date2num(
                        np.vstack((dt, coord.values)).T)
                    icoord.bounds = bnds
                else:
                    icoord.guess_bounds()
                cube.add_dim_coord(icoord, coord.dimension)
            else:
                icoord = AuxCoord(points=pts[i],
                                  standard_name=coord.name,
                                  coord_system=coord_sys,
                                  units=coord_units)
                if coord.name == 'time' and 'Av or Int period' in \
                        field_headings:
                    dt = coord.values - \
                        field_headings['Av or Int period']
                    bnds = time_unit.date2num(
                        np.vstack((dt, coord.values)).T)
                    icoord.bounds = bnds[i, :]
                cube.add_aux_coord(icoord)

        # Headings/column headings which are encoded elsewhere.
        headings = ['X', 'Y', 'Z', 'Time', 'Unit', 'Av or Int period',
                    'X grid origin', 'Y grid origin',
                    'X grid size', 'Y grid size',
                    'X grid resolution', 'Y grid resolution', ]

        # Add the Main Headings as attributes.
        for key, value in header.iteritems():
            if value is not None and value != '' and \
                    key not in headings:
                cube.attributes[key] = value

        # Add the Column Headings as attributes
        for key, value in field_headings.iteritems():
            if value is not None and value != '' and \
                    key not in headings:
                cube.attributes[key] = value

        yield cube


def load_NAMEIII_field(filename):
    """
    Load a NAME III grid output file returning a
    generator of :class:`iris.cube.Cube` instances.

    Args:

    * filename (string):
        Name of file to load.

    Returns:
        A generator :class:`iris.cube.Cube` instances.

    """
    # Loading a file gives a generator of lines which can be progressed using
    # the next() method. This will come in handy as we wish to progress
    # through the file line by line.
    with open(filename, 'r') as file_handle:
        # Create a dictionary which can hold the header metadata about this
        # file.
        header = read_header(file_handle)

        # Skip the next line (contains the word Fields:) in the file.
        file_handle.next()

        # Read the lines of column definitions.
        # In this version a fixed order of column headings is assumed (and
        # first 4 columns are ignored).
        column_headings = {}
        for column_header_name in ['Species Category', 'Name', 'Quantity',
                                   'Species', 'Unit', 'Sources', 'Ensemble Av',
                                   'Time Av or Int', 'Horizontal Av or Int',
                                   'Vertical Av or Int', 'Prob Perc',
                                   'Prob Perc Ens', 'Prob Perc Time',
                                   'Time', 'Z', 'D']:
            cols = [col.strip() for col in file_handle.next().split(',')]
            column_headings[column_header_name] = cols[4:-1]

        # Convert the time to python datetimes.
        new_time_column_header = []
        for i, t in enumerate(column_headings['Time']):
            dt = datetime.datetime.strptime(t, NAMEIII_DATETIME_FORMAT)
            new_time_column_header.append(dt)
        column_headings['Time'] = new_time_column_header

        # Convert averaging/integrating period to timedeltas.
        column_headings['Av or Int period'] = _calc_integration_period(
            column_headings['Time Av or Int'])

        # Build a time coordinate.
        tdim = NAMECoord(name='time', dimension=None,
                         values=np.array(column_headings['Time']))

        # Build regular latitude and longitude coordinates.
        lat, lon = _build_lat_lon_for_NAME_field(header)

        coords = [lon, lat, tdim]

        # Skip the line after the column headings.
        file_handle.next()

        # Create data arrays to hold the data for each column.
        n_arrays = header['Number of field cols']
        shape = (header['Y grid size'], header['X grid size'])
        data_arrays = _read_data_arrays(file_handle, n_arrays, shape)

    return _generate_cubes(header, column_headings, coords, data_arrays)


def load_NAMEII_field(filename):
    """
    Load a NAME II grid output file returning a
    generator of :class:`iris.cube.Cube` instances.

    Args:

    * filename (string):
        Name of file to load.

    Returns:
        A generator :class:`iris.cube.Cube` instances.

    """
    with open(filename, 'r') as file_handle:
        # Create a dictionary which can hold the header metadata about this
        # file.
        header = read_header(file_handle)

        # Origin in namever=2 format is bottom-left hand corner so alter this
        # to centre of a grid box
        header['X grid origin'] = header['X grid origin'] + \
            header['X grid resolution'] / 2
        header['Y grid origin'] = header['Y grid origin'] + \
            header['Y grid resolution'] / 2

        # Read the lines of column definitions.
        # In this version a fixed order of column headings is assumed (and
        # first 4 columns are ignored).
        column_headings = {}
        for column_header_name in ['Species Category', 'Species',
                                   'Time Av or Int', 'Quantity',
                                   'Unit', 'Z', 'Time']:
            cols = [col.strip() for col in file_handle.next().split(',')]
            column_headings[column_header_name] = cols[4:-1]

        # Convert the time to python datetimes
        new_time_column_header = []
        for i, t in enumerate(column_headings['Time']):
            dt = datetime.datetime.strptime(t, NAMEII_FIELD_DATETIME_FORMAT)
            new_time_column_header.append(dt)
        column_headings['Time'] = new_time_column_header

        # Convert averaging/integrating period to timedeltas.
        pattern = re.compile(r'\s*(\d{3})\s*(hr)?\s*(time)\s*(\w*)')
        column_headings['Av or Int period'] = []
        for i, t in enumerate(column_headings['Time Av or Int']):
            matches = pattern.search(t)
            hours = 0
            if matches:
                if len(matches.group(1)) > 0:
                    hours = float(matches.group(1))
            column_headings['Av or Int period'].append(
                datetime.timedelta(hours=hours))

        # Build a time coordinate.
        tdim = NAMECoord(name='time', dimension=None,
                         values=np.array(column_headings['Time']))

        # Build regular latitude and longitude coordinates.
        lat, lon = _build_lat_lon_for_NAME_field(header)

        coords = [lon, lat, tdim]

        # Skip the blank line after the column headings.
        file_handle.next()

        # Create data arrays to hold the data for each column.
        n_arrays = header['Number of fields']
        shape = (header['Y grid size'], header['X grid size'])
        data_arrays = _read_data_arrays(file_handle, n_arrays, shape)

    return _generate_cubes(header, column_headings, coords, data_arrays)


def load_NAMEIII_timeseries(filename):
    """
    Load a NAME III time series file returning a
    generator of :class:`iris.cube.Cube` instances.

    Args:

    * filename (string):
        Name of file to load.

    Returns:
        A generator :class:`iris.cube.Cube` instances.

    """
    with open(filename, 'r') as file_handle:
        # Create a dictionary which can hold the header metadata about this
        # file.
        header = read_header(file_handle)

        # skip the next line (contains the word Fields:) in the file.
        file_handle.next()

        # Read the lines of column definitions - currently hardwired
        column_headings = {}
        for column_header_name in ['Species Category', 'Name', 'Quantity',
                                   'Species', 'Unit', 'Sources', 'Ens Av',
                                   'Time Av or Int', 'Horizontal Av or Int',
                                   'Vertical Av or Int', 'Prob Perc',
                                   'Prob Perc Ens', 'Prob Perc Time',
                                   'Location', 'X', 'Y', 'Z', 'D']:
            cols = [col.strip() for col in file_handle.next().split(',')]
            column_headings[column_header_name] = cols[1:-1]

        # Determine the coordinates of the data and store in namedtuples.
        # Extract latitude and longitude information from X, Y location
        # headings.
        lat, lon = _build_lat_lon_for_NAME_timeseries(column_headings)

        # Convert averaging/integrating period to timedeltas.
        column_headings['Av or Int period'] = _calc_integration_period(
            column_headings['Time Av or Int'])

        # Skip the line after the column headings.
        file_handle.next()

        # Make a list of data lists to hold the data for each column.
        data_lists = [[] for i in range(header['Number of field cols'])]
        time_list = []

        # Iterate over the remaining lines which represent the data in a
        # column form.
        for line in file_handle:
            # Split the line by comma, removing the last empty column caused
            # by the trailing comma.
            vals = line.split(',')[:-1]

            # Time is stored in the first column.
            t = vals[0].strip()
            dt = datetime.datetime.strptime(t, NAMEIII_DATETIME_FORMAT)
            time_list.append(dt)

            # Populate the data arrays.
            for i, data_list in enumerate(data_lists):
                data_list.append(float(vals[i + 1]))

        data_arrays = [np.array(l) for l in data_lists]
        time_array = np.array(time_list)
        tdim = NAMECoord(name='time', dimension=0, values=time_array)

        coords = [lon, lat, tdim]

    return _generate_cubes(header, column_headings, coords, data_arrays)


def load_NAMEII_timeseries(filename):
    """
    Load a NAME II Time Series file returning a
    generator of :class:`iris.cube.Cube` instances.

    Args:

    * filename (string):
        Name of file to load.

    Returns:
        A generator :class:`iris.cube.Cube` instances.

    """
    with open(filename, 'r') as file_handle:
        # Create a dictionary which can hold the header metadata about this
        # file.
        header = read_header(file_handle)

        # Read the lines of column definitions.
        column_headings = {}
        for column_header_name in ['Y', 'X', 'Location',
                                   'Species Category', 'Species',
                                   'Quantity', 'Z', 'Unit']:
            cols = [col.strip() for col in file_handle.next().split(',')]
            column_headings[column_header_name] = cols[1:-1]

        # Determine the coordinates of the data and store in namedtuples.
        # Extract latitude and longitude information from X, Y location
        # headings.
        lat, lon = _build_lat_lon_for_NAME_timeseries(column_headings)

        # Skip the blank line after the column headings.
        file_handle.next()

        # Make a list of data arrays to hold the data for each column.
        data_lists = [[] for i in range(header['Number of series'])]
        time_list = []

        # Iterate over the remaining lines which represent the data in a
        # column form.
        for line in file_handle:
            # Split the line by comma, removing the last empty column caused
            # by the trailing comma.
            vals = line.split(',')[:-1]

            # Time is stored in the first two columns.
            t = (vals[0].strip() + ' ' + vals[1].strip())
            dt = datetime.datetime.strptime(
                t, NAMEII_TIMESERIES_DATETIME_FORMAT)
            time_list.append(dt)

            # Populate the data arrays.
            for i, data_list in enumerate(data_lists):
                data_list.append(float(vals[i + 2]))

        data_arrays = [np.array(l) for l in data_lists]
        time_array = np.array(time_list)
        tdim = NAMECoord(name='time', dimension=0, values=time_array)

        coords = [lon, lat, tdim]

    return _generate_cubes(header, column_headings, coords, data_arrays)


def load_NAMEIII_trajectory(filename):
    """
    Load a NAME III trajectory file returning a
    generator of :class:`iris.cube.Cube` instances.

    Args:

    * filename (string):
        Name of file to load.

    Returns:
        A generator :class:`iris.cube.Cube` instances.

    """
    time_unit = iris.unit.Unit('hours since epoch',
                               calendar=iris.unit.CALENDAR_GREGORIAN)

    with open(filename, 'r') as infile:
        header = read_header(infile)

        # read the column headings
        for line in infile:
            if line.startswith("    "):
                break
        headings = [heading.strip() for heading in line.split(",")]

        # read the columns
        columns = [[] for i in range(len(headings))]
        for line in infile:
            values = [v.strip() for v in line.split(",")]
            for c, v in enumerate(values):
                if "UTC" in v:
                    v = v.replace(":00 ", " ")  # Strip out milliseconds.
                    v = datetime.datetime.strptime(v, NAMEIII_DATETIME_FORMAT)
                else:
                    try:
                        v = float(v)
                    except ValueError:
                        pass
                columns[c].append(v)

    # Where's the Z column?
    z_column = None
    for i, heading in enumerate(headings):
        if heading.startswith("Z "):
            z_column = i
            break
    if z_column is None:
        raise iris.exceptions.TranslationError("Expected a Z column")

    # Every column up to Z becomes a coordinate.
    coords = []
    for name, values in izip(headings[:z_column+1], columns[:z_column+1]):
        values = np.array(values)
        if np.all(np.array(values) == values[0]):
            values = [values[0]]

        standard_name = long_name = units = None
        if isinstance(values[0], datetime.datetime):
            values = time_unit.date2num(values)
            units = time_unit
            if name == "Time":
                name = "time"
        elif " (Lat-Long)" in name:
            if name.startswith("X"):
                name = "longitude"
            elif name.startswith("Y"):
                name = "latitude"
            units = "degrees"
        elif name == "Z (m asl)":
            name = "height"
            units = "m"

        try:
            coord = DimCoord(values, units=units)
        except ValueError:
            coord = AuxCoord(values, units=units)
        coord.rename(name)
        coords.append(coord)

    # Every numerical column after the Z becomes a cube.
    for name, values in izip(headings[z_column+1:], columns[z_column+1:]):
        try:
            float(values[0])
        except ValueError:
            continue
        # units embedded in column heading?
        name, units = _split_name_and_units(name)
        cube = iris.cube.Cube(values, units=units)
        cube.rename(name)
        for coord in coords:
            dim = 0 if len(coord.points) > 1 else None
            if isinstance(coord, DimCoord) and coord.name() == "time":
                cube.add_dim_coord(coord.copy(), dim)
            else:
                cube.add_aux_coord(coord.copy(), dim)
        yield cube
