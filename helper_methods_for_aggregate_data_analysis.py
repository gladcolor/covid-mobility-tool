from covid_constants_and_util import *
# import statsmodels.api as sm
import json
import copy
# from fbprophet import Prophet
from collections import Counter
import re
import h5py
import ast
from shapely import wkt
from scipy.stats import pearsonr
import fiona
import geopandas
import csv
import os
from geopandas.tools import sjoin
import time
import scipy

import helper_methods_for_aggregate_data_analysis as helper


# automatically read weekly strings so we don't have to remember to update it each week.
ALL_WEEKLY_STRINGS = sorted([a.replace('-weekly-patterns.csv.gz', '') for a in os.listdir('/media/gpu/easystore/Safegraph/Weekly Places Patterns v2 (until 2020-06-15)/main-file/')])
try:
    cast_to_datetime = [datetime.datetime.strptime(s, '%Y-%m-%d') for s in ALL_WEEKLY_STRINGS]
except:
    print(ALL_WEEKLY_STRINGS)
    raise Exception("At least one weekly string is badly formatted.")


# Huan
def get_all_files(root_dir, ext_list):
    if not isinstance(ext_list, list):
        ext_list = [ext_list]
    all_files = []
    for root, dirs, files in os.walk(root_dir, topdown=False):
        for file in files:
            for target_ext in ext_list:
                ext_len = len(target_ext)
                ext = file[-ext_len:]
                if ext == target_ext:
                    all_files.append(os.path.join(root, file))
    return all_files

def load_social_distancing_metrics(datetimes, version='v2'):
    """
    Given a list of datetimes, load social distancing metrics for those days.

    load_social_distancing_metrics(helper.list_datetimes_in_range(datetime.datetime(2020, 3, 1),
                                                                  datetime.datetime(2020, 3, 7)))
    """
    print("Loading social distancing metrics for %i datetimes; using version %s" % (len(datetimes), version))
    t0 = time.time()
    daily_cols = ['device_count', 'distance_traveled_from_home', 'completely_home_device_count', 
                  'full_time_work_behavior_devices', 'median_home_dwell_time', 'median_non_home_dwell_time',
                  'median_percentage_time_home', 'destination_cbgs']
    concatenated_d = None
    for dt in datetimes:
        if version == 'v1':
            path = os.path.join('/dfs/scratch1/safegraph_homes/all_aggregate_data/daily_counts_of_people_leaving_homes/sg-social-distancing/',
                            dt.strftime('%Y/%m/%d/%Y-%m-%d-social-distancing.csv.gz'))
        elif version == 'v2':
#             path = os.path.join('/dfs/scratch1/safegraph_homes/all_aggregate_data/daily_counts_of_people_leaving_homes/social_distancing_v2/',
#                             dt.strftime('%Y/%m/%d/%Y-%m-%d-social-distancing.csv.gz'))
              path = os.path.join(CURRENT_DATA_DIR, 'Social Distancing Metrics v2.1 (formerly Physical Distancing Metrics)/%s' % dt.strftime('%Y/%m/%d/%Y-%m-%d-social-distancing.csv.gz'))
        else:
            raise Exception("Version should be v1 or v2")

        if os.path.exists(path):
            print("    loading: ", path)
            social_distancing_d = pd.read_csv(path, usecols=['origin_census_block_group'] + daily_cols)[['origin_census_block_group'] + daily_cols]
            social_distancing_d.columns = ['census_block_group'] + ['%i.%i.%i_%s' %
                                                                    (dt.year, dt.month, dt.day, a) for a in daily_cols]
            old_len = len(social_distancing_d)
            social_distancing_d = social_distancing_d.drop_duplicates(keep=False)
            n_dropped_rows = old_len - len(social_distancing_d)
            assert len(set(social_distancing_d['census_block_group'])) == len(social_distancing_d)
            assert(1.*n_dropped_rows/old_len < 0.002) # make sure not very many rows are duplicates.
            if version == 'v2':
                assert n_dropped_rows == 0 # they fixed the problem in v2.
            elif version == 'v1':
                assert n_dropped_rows > 0 # this seemed to be a problem in v1.

            if concatenated_d is None:
                concatenated_d = social_distancing_d
            else:
                concatenated_d = pd.merge(concatenated_d,
                                          social_distancing_d,
                                          how='outer',
                                          validate='one_to_one',
                                          on='census_block_group')
        else:
            raise Exception('Missing Social Distancing Metrics for %s' % dt.strftime('%Y/%m/%d'))
    if concatenated_d is None:  # could not find any of the dates
        return concatenated_d
    concatenated_d['census_block_group'] = concatenated_d['census_block_group'].astype(str).str.zfill(12)
    concatenated_d = concatenated_d.set_index('census_block_group')
    print("Total time to load social distancing metrics: %2.3f seconds; total rows %i" %
          (time.time() - t0, len(concatenated_d)))
    return concatenated_d

def annotate_with_demographic_info_and_write_out_in_chunks(full_df, just_testing=False):
    """
    Annotate the Safegraph POI data with Census data and other useful POI data. 
    """
    full_df['safegraph_place_id'] = full_df.index
    full_df.index = range(len(full_df))

    # merge with areas.
    safegraph_areas = pd.read_csv(PATH_TO_SAFEGRAPH_AREAS)
    print("Prior to merging with safegraph areas, %i rows" % len(full_df))
    safegraph_areas = safegraph_areas[['safegraph_place_id', 'area_square_feet']].dropna()
    safegraph_areas.columns = ['safegraph_place_id', 'safegraph_computed_area_in_square_feet']
    full_df = pd.merge(full_df, safegraph_areas, how='inner', on='safegraph_place_id', validate='one_to_one')
    print("After merging with areas, %i rows" % len(full_df))

    # map to demo info. The basic class we use here is CensusBlockGroups, which processes the Census data. 
    print("Mapping SafeGraph POIs to demographic info, including race and income.")
    gdb_files = ['ACS_2017_5YR_BG_51_VIRGINIA.gdb'] if just_testing else None
    cbg_mapper = CensusBlockGroups(base_directory='/dfs/scratch1/safegraph_homes/old_dfs_scratch0_directory_contents/new_census_data/', gdb_files=gdb_files)
    pop_df = load_dataframe_to_correct_for_population_size()
    chunksize = 100000

    annotated_df = []
    for chunk_number in range(len(full_df) // chunksize + 1):
        print("******************Annotating chunk %i" % chunk_number)
        start, end = chunk_number * chunksize, min((chunk_number + 1) * chunksize, len(full_df))
        d = full_df.iloc[start:end].copy()

        # Now annotate each POI on the basis of its location.
        mapped_pois = cbg_mapper.get_demographic_stats_of_points(d['latitude'].values,
                                          d['longitude'].values,
                                          desired_cols=['p_white', 'p_asian', 'p_black', 'median_household_income', 'people_per_mile'])
        mapped_pois['county_fips_code'] = mapped_pois['county_fips_code'].map(lambda x:int(x) if x is not None else x)
        mapped_pois.columns = ['poi_lat_lon_%s' % a for a in mapped_pois.columns]
        for c in mapped_pois.columns:
            d[c] = mapped_pois[c].values

        # Then annotate with demographic data based on where visitors come from (visitor_home_cbgs).
        d = aggregate_visitor_home_cbgs_over_months(d, population_df=pop_df)
        block_group_d = cbg_mapper.block_group_d.copy()
        block_group_d['id_to_match_to_safegraph_data'] = block_group_d['GEOID'].map(lambda x:x.split("US")[1]).astype(int)
        block_group_d = block_group_d[['id_to_match_to_safegraph_data', 'p_black', 'p_white', 'p_asian', 'median_household_income']]
        block_group_d = block_group_d.dropna()

        for col in block_group_d:
            if col == 'id_to_match_to_safegraph_data':
                continue
            cbg_dict = dict(zip(block_group_d['id_to_match_to_safegraph_data'].values, block_group_d[col].values))
            d['cbg_visitor_weighted_%s' % col] = d['aggregated_cbg_population_adjusted_visitor_home_cbgs'].map(lambda x:compute_weighted_mean_of_cbg_visitors(x, cbg_dict))

        # see how well we did.
        for c in [a for a in d.columns if 'poi_lat_lon_' in a or 'cbg_visitor_weighted' in a]:
            print("Have data for %s for fraction %2.3f of people" % (c, 1 - pd.isnull(d[c]).mean()))
        d.to_hdf(os.path.join(ANNOTATED_H5_DATA_DIR, CHUNK_FILENAME) ,f'chunk_{chunk_number}', mode='a', complevel=2)
        annotated_df.append(d)
    annotated_df = pd.concat(annotated_df)
    annotated_df.index = range(len(annotated_df))
    return annotated_df


def load_date_col_as_date(x):
    # we allow this to return None because sometimes we want to filter for cols which are dates.
    try:
        year, month, day = x.split('.')  # e.g., '2020.3.1'
        return datetime.datetime(int(year), int(month), int(day))             
    except:
        return None

def get_h5_filepath(load_backup):
    backup_string = 'BACKUP_' if load_backup else ''
    filepath = os.path.join(ANNOTATED_H5_DATA_DIR, backup_string + CHUNK_FILENAME)
    return filepath

def load_chunk(chunk, load_backup=False):
    """
    Load a single 100k chunk from the h5 file; chunks are randomized and so should be reasonably representative. 
    """
    filepath = get_h5_filepath(load_backup=load_backup)
    print("Reading chunk %i from %s" % (chunk, filepath))

    d = pd.read_hdf(filepath, key=f'chunk_{chunk}')
    date_cols = [load_date_col_as_date(a) for a in d.columns]
    date_cols = [a for a in date_cols if a is not None]
    print("Dates range from %s to %s" % (min(date_cols), max(date_cols)))
    return d

def load_multiple_chunks(chunks, load_backup=False, cols=None):
    """
    Loads multiple chunks from the h5 file. Currently quite slow; quicker if only a subset of columns are kept.
    Use the parameters cols to specify which columns to keep; if None then all are kept.
    """
    dfs = []
    for i in chunks:
        t0 = time.time()
        chunk = load_chunk(i, load_backup=load_backup)
        print("Loaded chunk %i in %2.3f seconds" % (i, time.time() - t0))
        if cols is not None:
            chunk = chunk[cols]
        dfs.append(chunk)
    t0 = time.time()
    df = pd.concat(dfs)
    print("Concatenated %d chunks in %2.3f seconds" % (len(chunks), time.time() - t0))
    return df

def load_all_chunks(cols=None, load_backup=False):
    """
    Load all 100k chunks from the h5 file. This currently takes a while.
    """
    filepath = get_h5_filepath(load_backup=load_backup)
    f = h5py.File(filepath, 'r')
    chunks = sorted([int(a.replace('chunk_', '')) for a in list(f.keys())])
    f.close()
    assert chunks == list(range(max(chunks) + 1))
    print("Loading all chunks: %s" % (','.join([str(a) for a in chunks])))
    return load_multiple_chunks(chunks, cols=cols, load_backup=load_backup)

def load_patterns_data(month=None, year=None, week_string=None, extra_cols=[], just_testing=False):
    """
    Load in Patterns data for a single month and year, or for a single week. (These options are mutually exclusive). 
    Use extra_cols to define non-default columns to load.

    just_testing is a flag to allow quicker prototyping; it will load only a subset of the data. 
    """
    change_by_date = ['visitor_home_cbgs', 'visitor_country_of_origin',
                      'distance_from_home', 'median_dwell', 'bucketed_dwell_times']  # fields that are time-varying

    if month is not None and year is not None:
        month_and_year = True
        assert week_string is None
        assert month in range(1, 13)
        assert year in [2017, 2018, 2019, 2020]
        if (year == 2019 and month == 12) or (year == 2020 and month in [1, 2]):
            upload_date_string = '2020-03-16'  # we originally downloaded files in two groups; load them in the same way.
        else:
            upload_date_string = '2019-12-12'
        month_and_year_string = '%i_%02d-%s' % (year, month, upload_date_string)
        base_dir = os.path.join(UNZIPPED_DATA_DIR, 'SearchofAllRecords-CORE_POI-GEOMETRY-PATTERNS-%s' % month_and_year_string)
        print("Loading all files from %s" % base_dir)

        filenames = [a for a in os.listdir(base_dir) if
                     (a.startswith('core_poi-geometry-patterns-part') and a.endswith('.csv.gz'))]

        # make sure we're not ignoring any files we don't expect to ignore. 
        assert all([a in ['brand_info.csv', 'visit_panel_summary.csv', 'README.txt', 'home_panel_summary.csv']
            for a in os.listdir(base_dir) if a not in filenames])
        if just_testing:
            filenames = filenames[:2]
        print("Number of files to load: %i" % len(filenames))
        full_paths = [os.path.join(base_dir, a) for a in filenames]
        x = load_csv_possibly_with_dask(full_paths, use_dask=True, usecols=['safegraph_place_id'
                                                                            'parent_safegraph_place_id',
                                                                            'location_name',
                                                                            'latitude',
                                                                            'longitude',
                                                                            'city',
                                                                            'region',
                                                                            'postal_code',
                                                                            'top_category',
                                                                            'sub_category',
                                                                            'naics_code',
                                                                            "polygon_wkt",
                                                                            "polygon_class",
                                                                            'visits_by_day',
                                                                            'visitor_home_cbgs',
                                                                            'visitor_country_of_origin',
                                                                            'distance_from_home',
                                                                            'median_dwell',
                                                                            'bucketed_dwell_times'] +
                                                                            extra_cols,
                                                                            dtype={'naics_code': 'float64'})
        print("Fraction %2.3f of NAICS codes are missing" % pd.isnull(x['naics_code']).mean())
        x = x.rename(columns={k: f'{year}.{month}.{k}' for k in change_by_date})
    else:
        # weekly patterns data. 
        month_and_year = False
        assert month is None and year is None
        assert week_string in ALL_WEEKLY_STRINGS
        filepath = os.path.join('/dfs/scratch1/safegraph_homes/all_aggregate_data/weekly_patterns_data/v1/main-file/%s-weekly-patterns.csv.gz' % week_string)
        # Filename is misleading - it is really a zipped file.
        # Also, we're missing some columns that we had before, so I think we're just going to have to join on SafeGraph ID.
        x = pd.read_csv(filepath, escapechar='\\', compression='gzip', nrows=10000 if just_testing else None, usecols=['safegraph_place_id',
            'visits_by_day',
            'visitor_home_cbgs',
            'visitor_country_of_origin',
            'distance_from_home',
            'median_dwell',
            'bucketed_dwell_times',
            'date_range_start',
            'visits_by_each_hour'])
        x['offset_from_gmt'] = x['date_range_start'].map(lambda x:x.split('-')[-1])
        assert x['date_range_start'].map(lambda x:x.startswith(week_string + 'T' + '00:00:00')).all() # make sure date range starts where we expect for all rows. 
        print("Offset from GMT value counts")
        print(x['offset_from_gmt'].value_counts())
        del x['date_range_start']
        x = x.rename(columns={k: f'{week_string}.{k}' for k in change_by_date})

    print("Prior to dropping rows with no visits by day, %i rows" % len(x))
    x = x.dropna(subset=['visits_by_day'])
    x['visits_by_day'] = x['visits_by_day'].map(json.loads) # convert string lists to lists.

    if month_and_year:
        days = pd.DataFrame(x['visits_by_day'].values.tolist(),
                     columns=[f'{year}.{month}.{day}'
                              for day in range(1, len(x.iloc[0]['visits_by_day']) + 1)])
    else:
        year = int(week_string.split('-')[0])
        month = int(week_string.split('-')[1])
        start_day = int(week_string.split('-')[2])
        start_datetime = datetime.datetime(year, month, start_day)
        all_datetimes = [start_datetime + datetime.timedelta(days=i) for i in range(7)]
        days = pd.DataFrame(x['visits_by_day'].values.tolist(),
                     columns=['%i.%i.%i' % (dt.year, dt.month, dt.day) for dt in all_datetimes])

        # Load hourly data as well.
        # Per SafeGraph documentation:
        # Start time for measurement period in ISO 8601 format of YYYY-MM-DDTHH:mm:SS±hh:mm
        # (local time with offset from GMT). The start time will be 12 a.m. Sunday in local time.
        x['visits_by_each_hour'] = x['visits_by_each_hour'].map(json.loads) # convert string lists to lists.
        assert all_datetimes[0].strftime('%A') == 'Sunday'
        hours = pd.DataFrame(x['visits_by_each_hour'].values.tolist(),
                     columns=[f'hourly_visits_%i.%i.%i.%i' % (dt.year, dt.month, dt.day, hour)
                              for dt in all_datetimes
                              for hour in range(0, 24)])

    days.index = x.index
    x = pd.concat([x, days], axis=1)
    if not month_and_year:
        assert list(x.index) == list(range(len(x)))
        assert (hours.index.values == x.index.values).all()
        hours.index = x.index
        old_len = len(x)
        x = pd.concat([x, hours], axis=1)
        assert len(x) == old_len
        x = x.drop(columns=['visits_by_each_hour'])

        # The hourly data has some spurious spikes
        # related to the GMT-day boundary which we have to correct for.
        date_cols = [load_date_col_as_date(a) for a in x.columns]
        date_cols = [a for a in date_cols if a is not None]
        assert len(date_cols) == 7

        if week_string >= '2020-03-15': # think this is because of DST. Basically, these are the timezone strings we look for and correct; they shift at DST. 
            hourly_offsets = [4, 5, 6, 7]
        else:
            hourly_offsets = [5, 6, 7, 8]
        hourly_offset_strings = ['0%i:00' % hourly_offset for hourly_offset in hourly_offsets]

        percent_rows_being_corrected = (x['offset_from_gmt'].map(lambda a:a in hourly_offset_strings).mean() * 100)
        print("%2.3f%% of rows have timezones that we spike-correct for." % percent_rows_being_corrected) 
        assert percent_rows_being_corrected > 99 # make sure we're correcting almost all rows

        # have to correct for each timezone separately.
        for hourly_offset in hourly_offsets:
            idxs = x['offset_from_gmt'] == ('0%i:00' % hourly_offset)
            for date_col in date_cols: # loop over days.
                date_string = '%i.%i.%i' % (date_col.year, date_col.month, date_col.day)
                # not totally clear which hours are messed up - it's mainly one hour, but the surrounding ones look weird too -
                # or what the best way to interpolate is, but this yields plots which look reasonable.

                for hour_to_correct in [24 - hourly_offset - 1,
                                        24 - hourly_offset,
                                        24 - hourly_offset + 1]:

                    # interpolate using hours fairly far from hour_to_correct to avoid pollution.
                    if hour_to_correct < 21:
                        cols_to_use = ['hourly_visits_%s.%i' % (date_string, a) for a in [hour_to_correct - 3, hour_to_correct + 3]]
                    else:
                        # Use smaller offset so we don't have hours >= 24. This technically overlaps with earlier hours, 
                        # but I think it should be okay because they will already have been corrected. 
                        cols_to_use = ['hourly_visits_%s.%i' % (date_string, a) for a in [hour_to_correct - 2, hour_to_correct + 2]]
                    assert all([col in x.columns for col in cols_to_use])
                    x.loc[idxs, 'hourly_visits_%s.%i' % (date_string, hour_to_correct)] = x.loc[idxs, cols_to_use].mean(axis=1)
        del x['offset_from_gmt']
    x = x.set_index('safegraph_place_id')
    x = x.drop(columns=['visits_by_day'])

    if month_and_year:
        print("%i rows loaded for month and year %s" % (len(x), month_and_year_string))
    else:
        print("%i rows loaded for week %s" % (len(x), week_string))
    return x

def load_weekly_patterns_v2_data(week_string, cols_to_keep=[], expand_hourly_visits=True, path_to_csv=None):
    """
    week_string: csv file name (released date)
    Load in Weekly Patterns V2 data for a single week. 
    If week_string <= '2020-06-15': we are using the earlier version of Weekly Pattern v2, and 
                                    week_string denotes the first day of the week.
    Else: we are using the later version of Weekly Patterns v2, and week_string denotes the day this update was released.
    """

    if len(cols_to_keep) == 0:
        cols_to_keep = ['safegraph_place_id', 'poi_cbg', 'visitor_home_cbgs', 'visitor_daytime_cbgs',
                                     'visitor_country_of_origin', 'distance_from_home', 'median_dwell',
                                     'bucketed_dwell_times']

    # make sure the week_string is correct.
    ts = time.time()
    elements = week_string.split('-')
    assert len(elements) == 3
    week_datetime = datetime.datetime(int(elements[0]), int(elements[1]), int(elements[2]))
    cols_to_load = cols_to_keep.copy()
    must_load_cols = ['date_range_start', 'visits_by_each_hour']  # required for later logic
    for k in must_load_cols:
        if k not in cols_to_load:
            cols_to_load.append(k)


    if week_string <= '2020-06-15':
        path_to_csv = os.path.join(CURRENT_DATA_DIR, 'weekly_pre_20200615/main-file/%s-weekly-patterns.csv.gz' % week_string)
        assert os.path.isfile(path_to_csv)
        print('Loading from %s' % path_to_csv)
        df = load_csv_possibly_with_dask(path_to_csv, use_dask=True, usecols=cols_to_load, dtype={'poi_cbg':'float64'})
        start_day_string = week_string
        start_datetime = week_datetime

    # week_string <= '2020-06-15'
    else:
        print("week_string:", week_string)
        if week_string <= '2020-12-08':  # this is release date; start of this week is 2020-11-30
            path_to_weekly_dir = os.path.join(WEEKLY_PATTERNS_BEFORE_20201130, r'/%s/' % week_datetime.strftime('%Y/%m/%d'))

        # The above code have not modified! Huan
        # week_string > '2020-12-08'
        else:
            #date_dir = os.path.join(CURRENT_DATA_DIR, 'Weekly Places Patterns (for data from 2020-11-30 to Present)/patterns/%s/' % week_datetime.strftime('%Y/%m/%d'))
            ##hour_dir = os.listdir(date_dir)[0]
            #week_dir = os.path.join(date_dir, hour_dir)
            path_to_weekly_dir = os.path.join(WEEKLY_PATTERNS_AFTER_20201130 + '/%s/' % week_datetime.strftime('%Y/%m/%d'))
            # print(f"WEEKLY_PATTERNS_AFTER_20201130: {WEEKLY_PATTERNS_AFTER_20201130}")
        inner_folder = os.listdir(path_to_weekly_dir)
        assert len(inner_folder) == 1  # there is always a single folder inside the weekly folder 
        path_to_patterns_parts = os.path.join(path_to_weekly_dir, inner_folder[0])
        dfs = []
        for filename in sorted(os.listdir(path_to_patterns_parts)):
            if filename.startswith('patterns-part'):  # e.g., patterns-part1.csv.gz
                path_to_csv = os.path.join(path_to_patterns_parts, filename)
                assert os.path.isfile(path_to_csv)
                print('Loading from %s' % path_to_csv)
                df = load_csv_possibly_with_dask(path_to_csv, use_dask=False, usecols=cols_to_load, dtype={'poi_cbg':str})
                #df = df[df['poi_cbg'].astype(str).str[:2] == '45']
                dfs.append(df)
        df = pd.concat(dfs, axis=0)
        del dfs # to save some memory
        start_day_string = df.iloc[0].date_range_start.split('T')[0]
        print('Actual start of the week:', start_day_string)
        elements = start_day_string.split('-')
        assert len(elements) == 3
        start_datetime = datetime.datetime(int(elements[0]), int(elements[1]), int(elements[2]))
    assert df['date_range_start'].map(lambda x:x.startswith(start_day_string + 'T00:00:00')).all()  # make sure date range starts where we expect for all rows.     

    # the dataframe is ready by the above code.
    # Start to process dataframe
    if expand_hourly_visits:     # expand single hourly visits column into one column per hour
        df['visits_by_each_hour'] = df['visits_by_each_hour'].map(json.loads) # convert string lists to lists.
        all_dates = [start_datetime + datetime.timedelta(days=i) for i in range(7)]  # all days in the week
        hours = pd.DataFrame(df['visits_by_each_hour'].values.tolist(),
                     columns=[f'hourly_visits_%i.%i.%i.%i' % (date.year, date.month, date.day, hour)
                              for date in all_dates   # 7 days in the week
                              for hour in range(0, 24)])  # 0 - 23 hour
        assert len(hours) == len(df)
        hours.index = df.index  # safegraph_place_id
        df = pd.concat([df, hours], axis=1)
        # The hourly data has some spurious spikes
        # related to the GMT-day boundary which we have to correct for.
        df['offset_from_gmt'] = df['date_range_start'].map(lambda x:x[len(start_day_string + 'T00:00:00'):])
        print("Offset from GMT value counts")
        offset_counts = df['offset_from_gmt'].value_counts()
        print(offset_counts)
        hourly_offset_strings = offset_counts[:4].index  # four most common timezones across POIs
        #
        assert all(['-0%i:00' % x in hourly_offset_strings for x in [5, 6, 7]])  # should always include GMT-5, -6, -7
        assert ('-04:00' in hourly_offset_strings) or ('-08:00' in hourly_offset_strings)  # depends on DST 
        percent_rows_being_corrected = (df['offset_from_gmt'].map(lambda x:x in hourly_offset_strings).mean() * 100)
        print("%2.3f%% of rows have timezones that we spike-correct for." % percent_rows_being_corrected) 
        assert percent_rows_being_corrected > 98  # almost all rows should fall in these timezones
        end_datetime = datetime.datetime(all_dates[-1].year, all_dates[-1].month, all_dates[-1].day, 23)

        # have to correct for each timezone separately.
        # Huan: don't understant yet.
        for offset_string in sorted(hourly_offset_strings):  # four most common timezones across POIs
            print('Correcting GMT%s...' % offset_string)   # offset_string: -05:00, -06:00, -07:00, , -08:00
            idxs = df['offset_from_gmt'] == offset_string
            offset_int = int(offset_string.split(':')[0])
            assert (-8 <= offset_int) and (offset_int <= -4)  # -5, -6, -7, , -8
            for date in all_dates:   # all days in the week, 7 days, e.g.: 2020-12-14
                # not totally clear which hours are messed up - it's mainly one hour, but the surrounding ones 
                # look weird too - but this yields plots which look reasonable.
                for hour_to_correct in [24 + offset_int - 1,
                                        24 + offset_int,
                                        24 + offset_int + 1]:
                    # interpolate using hours fairly far from hour_to_correct to avoid pollution.
                    dt_hour_to_correct = datetime.datetime(date.year, date.month, date.day, hour_to_correct)
                    start_hour = max(start_datetime, dt_hour_to_correct + datetime.timedelta(hours=-3))
                    end_hour = min(end_datetime, dt_hour_to_correct + datetime.timedelta(hours=3))
                    cols_to_use = [f'hourly_visits_%i.%i.%i.%i' % (dt.year, dt.month, dt.day, dt.hour) for dt in list_hours_in_range(start_hour, end_hour)]
                    assert all([col in df.columns for col in cols_to_use])
                    # this technically overlaps with earlier hours, but it should be okay because they will 
                    # already have been corrected.
                    # Huan don't understand
                    df.loc[idxs, 'hourly_visits_%i.%i.%i.%i' % (date.year, date.month, date.day, hour_to_correct)] = df.loc[idxs, cols_to_use].mean(axis=1)
                    # will bring some decimal numbers.
    
    non_required_cols = [col for col in df.columns if not(col in cols_to_keep or col.startswith('hourly_visits_'))]
    df = df.drop(columns=non_required_cols)
    df = df.set_index('safegraph_place_id')
    te = time.time()
    print("%i rows loaded for week %s [total time = %.2fs]" % (len(df), start_day_string, te-ts))
    return df

def load_core_places_footprint_data(cols_to_keep):
    area_csv = os.path.join(FOOTPRINT_FILE)
    print('Loading', area_csv)
    df = load_csv_possibly_with_dask(area_csv, usecols=cols_to_keep, use_dask=False)
    df = df.set_index('safegraph_place_id')
    print('Loaded core places footprint data for %d POIs' % len(df))
    return df

def load_core_places_data(cols_to_keep):
    core_dir = os.path.join(CORE_POI_DIR)  # use the most recent core info
    dfs = []
    for filename in sorted(os.listdir(core_dir)):
        if filename.startswith('core_poi-part'):

            path_to_csv = os.path.join(core_dir, filename)


            print('Loading', path_to_csv)

            path_to_csv = [path_to_csv]  # Huan
            # df = load_csv_possibly_with_dask(path_to_csv, usecols=cols_to_keep, use_dask=True)
            df = load_csv_possibly_with_dask(path_to_csv, usecols=cols_to_keep, use_dask=False)  # Huan
            dfs.append(df)
    df = pd.concat(dfs, axis=0)
    df = df.set_index('safegraph_place_id')
    print('Loading core places info for %d POIs' % len(df))
    return df

def load_google_mobility_data(only_US=True):
    df = pd.read_csv(PATH_TO_GOOGLE_DATA)
    if only_US:
        df = df[df['country_region_code'] == 'US']
    return df

def list_datetimes_in_range(min_day, max_day):
    """
    Return a list of datetimes in a range from min_day to max_day, inclusive. Increment is one day. 
    """
    assert(min_day <= max_day)
    days = []
    while min_day <= max_day:
        days.append(min_day)
        min_day = min_day + datetime.timedelta(days=1)
    return days 

def list_hours_in_range(min_hour, max_hour):
    """
    Return a list of datetimes in a range from min_hour to max_hour, inclusive. Increment is one hour. 
    """
    assert(min_hour <= max_hour), f'min_hour: {min_hour}, max_hour: {max_hour}'
    hours = []
    while min_hour <= max_hour:
        hours.append(min_hour)
        min_hour = min_hour + datetime.timedelta(hours=1)
    return hours

def normalize_dict_values_to_sum_to_one_and_cast_keys_to_ints(old_dict):
    """
    Self-explanatory; used by aggregate_visitor_home_cbgs_over_months.
    """
    new_dict = {}
    value_sum = 1.*sum(old_dict.values())
    if len(old_dict) > 0:
        assert value_sum > 0
    for k in old_dict:
        new_dict[int(k)] = old_dict[k] / value_sum
    return new_dict

def cast_keys_to_ints(old_dict, verbose=True):
    new_dict = {}
    for k in old_dict:
        # Huan remove "CA:XXX" (CA means Canada)
        if str(k).isnumeric():
            new_dict[int(k)] = old_dict[k]
        else:
            if verbose:
                print(f"cast_keys_to_ints(): {k} is not numeric, skipped it.")
    return new_dict

def aggregate_visitor_home_cbgs_over_months(d, cutoff_year=2019, population_df=None, periods_to_include=None):
    """
    Aggregate visitor_home_cbgs across months and produce a normalized aggregate field.

    Usage: d = aggregate_visitor_home_cbgs_over_months(d).
    cutoff = the earliest time (could be year or year.month) to aggregate data from
    population_df = the DataFrame loaded by load_dataframe_to_correct_for_population_size
    return: row: blockgroup, column: weekly POI visitation
    """
    t0 = time.time()
    if periods_to_include is not None:
        cols = ['%s.visitor_home_cbgs' % period for period in periods_to_include]
        assert cutoff_year is None
    else:
        # Not using CBG data from weekly files for now because of concerns that it's inconsistently
        # processed - they change how they do the privacy filtering.
        assert cutoff_year is not None
        weekly_cols_to_exclude = ['%s.visitor_home_cbgs' % a for a in ALL_WEEKLY_STRINGS]
        cols = [a for a in d.columns if (a.endswith('.visitor_home_cbgs') and (a >= str(cutoff_year)) and (a not in weekly_cols_to_exclude))]
    print('Aggregating data from: %s' % cols)
    assert all([a in d.columns for a in cols])

    # Helper variables to use if visitor_home_cbgs counts need adjusting for differential sampling across CBGs. 
    adjusted_cols = []
    if population_df is not None:
        # int_cbgs = [int(cbg) for cbg in population_df.census_block_group]
        int_cbgs = [str(cbg) for cbg in population_df.census_block_group]   # Huan

    for k in cols:
        if type(d.iloc[0][k]) != Counter:
            print('Filling %s with Counter objects' % k)  # d: core_poi_df
            d[k] = d[k].fillna('{}').map(lambda x:Counter(cast_keys_to_ints(json.loads(x))))  # map strings to counters.
        if population_df is not None:
            sub_t0 = time.time()
            new_col = '%s_adjusted' % k
            total_population = population_df.total_cbg_population.to_numpy()
            time_period = k.strip('.visitor_home_cbgs')
            population_col = 'number_devices_residing_%s' % time_period
            assert(population_col in population_df.columns)
            num_devices = population_df[population_col].to_numpy()
            assert np.isnan(num_devices).sum() == 0
            assert np.isnan(total_population).sum() == 0
            cbg_coverage = num_devices / total_population
            median_coverage = np.nanmedian(cbg_coverage)
            cbg_coverage = dict(zip(int_cbgs, cbg_coverage))
            assert ~np.isnan(median_coverage)
            assert ~np.isinf(median_coverage)
            assert median_coverage > 0.001
            # if median_coverage > 0.001:
            #     print(f'WARNING: median_coverage {median_coverage:.4f} is large than 0.001!') # Huan
            # want to make sure we aren't mis
            # sing data for too many CBGs, so a small hack - have
            # adjust_home_cbg_counts_for_coverage return two arguments, where the second argument
            # tells us if we had to clip or fill in the missing coverage number.
            d[new_col] = d[k].map(lambda x:adjust_home_cbg_counts_for_coverage(x, cbg_coverage, median_coverage=median_coverage))
            print('Finished adjusting home CBG counts for %s [time=%.3fs] had to fill in or clip coverage for %2.6f%% of rows; in those cases used median coverage %2.3f' %
                  (time_period, time.time() - sub_t0, 100 * d[new_col].map(lambda x:x[1]).mean(), median_coverage))
            d[new_col] = d[new_col].map(lambda x:x[0]) # remove the second argument of adjust_home_cbg_counts_for_coverage, we don't need it anymore.
            adjusted_cols.append(new_col)

            # make sure there are no NAs anywhere. 
            assert d[k].map(lambda x:len([a for a in x.values() if np.isnan(a)])).sum() == 0
            assert d[new_col].map(lambda x:len([a for a in x.values() if np.isnan(a)])).sum() == 0

    # add counters together across months.
    d['aggregated_visitor_home_cbgs'] = d[cols].aggregate(func=sum, axis=1)
    # normalize each counter so its values sum to 1.
    d['aggregated_visitor_home_cbgs'] = d['aggregated_visitor_home_cbgs'].map(normalize_dict_values_to_sum_to_one_and_cast_keys_to_ints)

    if len(adjusted_cols) > 0:
        d['aggregated_cbg_population_adjusted_visitor_home_cbgs'] = d[adjusted_cols].aggregate(func=sum, axis=1)
        d['aggregated_cbg_population_adjusted_visitor_home_cbgs'] = d['aggregated_cbg_population_adjusted_visitor_home_cbgs'].map(normalize_dict_values_to_sum_to_one_and_cast_keys_to_ints)
        d = d.drop(columns=adjusted_cols)

    for k in ['aggregated_cbg_population_adjusted_visitor_home_cbgs', 
          'aggregated_visitor_home_cbgs']:
        y = d.loc[d[k].map(lambda x:len(x) > 0), k]
        y = y.map(lambda x:sum(x.values()))

        print('y:', y.isna().sum())
        assert np.allclose(y, 1)

    print("Aggregating CBG visitors over %i time periods took %2.3f seconds" % (len(cols), time.time() - t0))
    print("Fraction %2.3f of POIs have CBG visitor data" % (d['aggregated_visitor_home_cbgs'].map(lambda x:len(x) != 0).mean()))
    return d

def adjust_home_cbg_counts_for_coverage(cbg_counter, cbg_coverage, median_coverage, max_upweighting_factor=100):
    """
    Adjusts the POI-CBG counts from SafeGraph to estimate the true count, based on the
    coverage that SafeGraph has for this CBG.
    cbg_counter: a Counter object mapping CBG to the original count
    cbg_coverage: a dictionary where keys are CBGs and each data point represents SafeGraph's coverage: num_devices / total_population
    This should be between 0 and 1 for the vast majority of cases, although for some weird CBGs it may not be.
    Returns the adjusted dictionary and a Bool flag had_to_guess_coverage_value which tells us whether we had to adjust the coverage value.
    """
    had_to_guess_coverage_value = False
    if len(cbg_counter) == 0:
        return cbg_counter, had_to_guess_coverage_value
    new_counter = Counter()
    for cbg in cbg_counter:
        # cover some special cases which should happen very rarely. 
        if cbg not in cbg_coverage:
            upweighting_factor = 1 / median_coverage
            had_to_guess_coverage_value = True
        elif np.isnan(cbg_coverage[cbg]): # not sure this case ever actually happens, but just in case. 
            upweighting_factor = 1 / median_coverage
            had_to_guess_coverage_value = True
        else: 
            assert cbg_coverage[cbg] >= 0
            if cbg_coverage[cbg] == 0:
                upweighting_factor = max_upweighting_factor
            else:
                upweighting_factor = min(1 / cbg_coverage[cbg], max_upweighting_factor)  # need to invert coverage
        new_counter[cbg] = cbg_counter[cbg] * upweighting_factor
    return new_counter, had_to_guess_coverage_value

def compute_weighted_mean_of_cbg_visitors(cbg_visitor_fracs, cbg_values):
    """
    Given a dictionary cbg_visitor_fracs which gives the fraction of people from a CBG which visit a POI
    and a dictionary cbg_values which maps CBGs to values, compute the weighted mean for the POI.
    """
    if len(cbg_visitor_fracs) == 0:
        return None
    else:
        numerator = 0.
        denominator = 0.
        for cbg in cbg_visitor_fracs:
            if cbg not in cbg_values:
                continue
            numerator += cbg_visitor_fracs[cbg] * cbg_values[cbg]
            denominator += cbg_visitor_fracs[cbg]
        if denominator == 0:
            return None
        return numerator/denominator

def load_dataframe_for_individual_msa(MSA_name, version='v2', time_period=None, nrows=None):
    """
    This loads all the POI info for a single MSA.
    """
    t0 = time.time()
    if version == 'v1':
        assert time_period is None
        filename = os.path.join(STRATIFIED_BY_AREA_DIR, '%s.csv' % MSA_name)
        d = pd.read_csv(filename, nrows=nrows)
        for k in (['aggregated_cbg_population_adjusted_visitor_home_cbgs', 'aggregated_visitor_home_cbgs']):
            d[k] = d[k].map(lambda x:cast_keys_to_ints(json.loads(x)))
        for k in ['%s.visitor_home_cbgs' % a for a in ALL_WEEKLY_STRINGS]:
            d[k] = d[k].fillna('{}')
            d[k] = d[k].map(lambda x:cast_keys_to_ints(json.loads(x)))
    else:
        assert version == 'v2'
        if time_period is None:  # want time-aggregated
            agg_dir = os.path.join(NEW_STRATIFIED_BY_AREA_DIR, 'time_aggregated/')
            filename = None
            for fn in os.listdir(agg_dir):  # find the CSV file with according to the date span.  # Huan
                if fn.endswith('%s.csv' % MSA_name):
                    filename = os.path.join(agg_dir, fn)
                    break
            d = pd.read_csv(filename)

            for k in d.columns:
                if k.endswith('aggregated_cbg_population_adjusted_visitor_home_cbgs') or k.endswith('aggregated_visitor_home_cbgs'):
                    print(f"cast_keys_to_ints column {k} ...")
                    d[k] = d[k].map(lambda x:cast_keys_to_ints(json.loads(x)))
                    # print(d[k])
        else:
            filename = os.path.join(NEW_STRATIFIED_BY_AREA_DIR, '%s/%s.csv' % (time_period, MSA_name))
            print('filename:', filename)
            assert os.path.isfile(filename), f"{filename} is not exist."

            d = pd.read_csv(filename)


    print("Loaded %i rows for %s in %2.3f seconds" % (len(d), MSA_name, time.time() - t0))
    #d.loc[:, 'poi_cbg'] = d['poi_cbg'].astype(int).astype(str).str.zfill(12)
    try:
        d.set_index('safegraph_place_id', inplace=True)
    except:
        pass
    return d

def prep_msa_df_for_model_experiments(msa_name, time_period_strings=None):
    """
    Loads the core and weekly POI information for this MSA, and renames columns as they're expected in
    fit_disease_model_on_real_data.
    """
    all_msa_names = msa_name.split('+')  # sometimes msa_name is actually multiple MSAs joined by '+'
    all_msa_dfs = []
    for msa_name in all_msa_names:
        merged_df = load_dataframe_for_individual_msa(msa_name, version='v2', time_period=None)
        # change column names to fit model experiments code
        # Huan do not understand.
        merged_df = merged_df.rename(columns={'area_square_feet':'safegraph_computed_area_in_square_feet',
                                              # '20191230_20201019_aggregated_visitor_home_cbgs':'aggregated_cbg_population_adjusted_visitor_home_cbgs',
                                              '20201209_20210630_aggregated_visitor_home_cbgs':'aggregated_cbg_population_adjusted_visitor_home_cbgs',
                                              # '20191230_20201019_median_of_median_dwell':'avg_median_dwell'})
                                              '20201209_20210630_median_of_median_dwell':'avg_median_dwell'})
        # merged_df['aggregated_cbg_population_adjusted_visitor_home_cbgs'] = merged_df['20201209_20210630_aggregated_cbg_population_adjusted_visitor_home_cbgs']  # Huan
        # merged_df['median_of_median_dwell'] = merged_df['20201209_20210630_median_of_median_dwell']
        # merged_df['median_of_median_dwell'] = merged_df['20201209_20210630_median_of_median_dwell']
        # merged_df: witouhd hourly visitation
        #
        if time_period_strings is not None:
            for ts in time_period_strings:  # get hourly info from time-specific dataframes
                time_specific_df = load_dataframe_for_individual_msa(msa_name, version='v2', time_period=ts)
                # time_specific_df: have hour visitation
                hourly_cols = [col for col in time_specific_df.columns if col.startswith('hourly_visits')]
                merged_df = pd.merge(merged_df, time_specific_df[hourly_cols], how='left', left_index=True, right_index=True, validate='one_to_one')        
        all_msa_dfs.append(merged_df)
    msa_df = pd.concat(all_msa_dfs)
    duplicated = msa_df.index.duplicated(keep='first')
    num_dupes = np.sum(duplicated)
    if num_dupes > 10:  # dupes should be very rare
        raise Exception('Found %d duplicated POIs after concatenating MSA dataframes' % num_dupes)
    if num_dupes > 0:
        print('Found %d duplicated POIs after concatenating MSA dataframes' % num_dupes)
    msa_df = msa_df[~duplicated]
    return msa_df

def load_dataframe_to_correct_for_population_size(version='v2', just_load_census_data=False, 
                                                  min_date_string=None, max_date_string=None, verbose=True):
    """
    Load in a dataframe with rows for the 2018 ACS Census population code in each CBG
    and the SafeGraph population count in each CBG (from home-panel-summary.csv). 
    The correlation is not actually that good, likely because individual CBG counts are noisy. 

    Definition of
    num_devices_residing: Number of distinct devices observed with a primary nighttime location in the specified census block group.
    """


    assert version in {'v1', 'v2'}
    # acs_data = pd.read_csv(PATH_TO_ACS_1YR_DATA,
    #                       encoding='cp1252',
    #                    usecols=['STATEA', 'COUNTYA', 'TRACTA', 'BLKGRPA','AJWBE001'],
    #                    dtype={'STATEA':str,
    #                           'COUNTYA':str,
    #                           'BLKGRPA':str,
    #                          'TRACTA':str})

    acs_data = pd.read_csv(PATH_TO_ACS_5YR_DATA, engine='python') # acs_data: blockgroups, row: blockgroup,
    # column: GEOID, income, black/white_race_ratio,  Huan
    # acs_data = pd.read_csv(PATH_TO_ACS_1YR_DATA,
    '''
    acs_data = pd.read_csv(PATH_TO_ACS_5YR_DATA,
                          encoding='cp1252',
                       usecols=['STATEA', 'COUNTYA', 'TRACTA', 'BLKGRPA','AJWBE001'],
                       dtype={'STATEA':str,
                              'COUNTYA':str,
                              'BLKGRPA':str,
                             'TRACTA':str})

    # https://www.census.gov/programs-surveys/geography/guidance/geo-identifiers.html
    # FULL BLOCK GROUP CODE = STATE+COUNTY+TRACT+BLOCK GROUP
    assert (acs_data['STATEA'].map(len) == 2).all()
    assert (acs_data['COUNTYA'].map(len) == 3).all()
    assert (acs_data['TRACTA'].map(len) == 6).all()
    assert (acs_data['BLKGRPA'].map(len) == 1).all()
    acs_data['census_block_group'] = (acs_data['STATEA'] +
                                    acs_data['COUNTYA'] +
                                    acs_data['TRACTA'] +
                                    acs_data['BLKGRPA'])
    acs_data['census_block_group'] = acs_data['census_block_group'].astype(int)
    assert len(set(acs_data['census_block_group'])) == len(acs_data)
    acs_data['county_code'] = (acs_data['STATEA'] + acs_data['COUNTYA']).astype(int)
    acs_data = acs_data[['census_block_group', 'AJWBE001', 'STATEA', 'county_code']]
    acs_data = acs_data.rename(mapper={'AJWBE001':'total_cbg_population',
                                       'STATEA':'state_code'}, axis=1)
    print("%i rows of 2018 1-year ACS data read" % len(acs_data))    
    '''

    # acs_data['census_block_group'] = acs_data['GEOID'].astype(int)
    acs_data['census_block_group'] = acs_data['GEOID'].astype(str).str[-12:]
    #print("acs_data['census_block_group']:", acs_data['census_block_group'])
    acs_data['total_cbg_population'] = acs_data['cbg_total_population']

    if just_load_census_data:
        return acs_data
    combined_data = acs_data

    # now read in safegraph data to use as normalizer. Months and years first.
    all_filenames = []
    all_date_strings = []
    if version == 'v1':
        for month, year in [(1, 2017),(2, 2017),(3, 2017),(4, 2017),(5, 2017),(6, 2017),(7, 2017),(8, 2017),(9, 2017),(10, 2017),(11, 2017),(12, 2017),
                 (1, 2018),(2, 2018),(3, 2018),(4, 2018),(5, 2018),(6, 2018),(7, 2018),(8, 2018),(9, 2018),(10, 2018),(11, 2018),(12, 2018),
                 (1, 2019),(2, 2019),(3, 2019),(4, 2019),(5, 2019),(6, 2019),(7, 2019),(8, 2019),(9, 2019),(10, 2019),(11, 2019),(12, 2019),
                 (1, 2020),(2, 2020)]:
            if (year == 2019 and month == 12) or (year == 2020 and month in [1, 2]):
                upload_date_string = '2020-03-16'  # we downloaded files in two groups; load them in the same way.
            else:
                upload_date_string = '2019-12-12'
            month_and_year_string = '%i_%02d-%s' % (year, month, upload_date_string)
            filename = os.path.join(UNZIPPED_DATA_DIR,
                                    'SearchofAllRecords-CORE_POI-GEOMETRY-PATTERNS-%s' % month_and_year_string,
                                    'home_panel_summary.csv')
            all_filenames.append(filename)
            all_date_strings.append('%i.%i' % (year, month))

        # now weeks
        for date_string in ALL_WEEKLY_STRINGS:
            all_filenames.append(
                '/dfs/scratch1/safegraph_homes/all_aggregate_data/weekly_patterns_data/v1/home_summary_file/%s-home-panel-summary.csv' % date_string)
            all_date_strings.append(date_string)
    else:
        # path_to_weekly_v2_pt1 = os.path.join(CURRENT_DATA_DIR, '/media/gpu/easystore/Safegraph/Weekly Places Patterns Backfill for Dec 2020 and Onward Release/home_panel_summary_backfill/2020/12/14/21/')
        # for filename in os.listdir(path_to_weekly_v2_pt1):
        #     date_string = filename[:-len('-home-panel-summary.csv')]
        #     if min_date_string is None or date_string >= min_date_string:
        #         if max_date_string is None or date_string <= max_date_string:
        #             all_filenames.append(os.path.join(path_to_weekly_v2_pt1, filename))
        #             all_date_strings.append(date_string)
        # path_to_weekly_v2_pt1 = os.path.join(CURRENT_DATA_DIR, '/media/gpu/easystore/Safegraph/Weekly Places Patterns (for data from 2020-11-30 to Present)/home_panel_summary/')
        # print("path_to_weekly_v2_pt1", path_to_weekly_v2_pt1)
        # all_home_panel_summary_files = get_all_files(path_to_weekly_v2_pt1, '.csv')
        # # for filename in os.listdir(path_to_weekly_v2_pt1):
        # for filename in all_home_panel_summary_files:
        #     # date_string = filename[:-len('-home-panel-summary.csv')]
        #     tag_str = r'home_panel_summary'
        #     tag_pos = filename.find(tag_str) + len(tag_str)
        #     date_string = filename[tag_pos+1:tag_pos + 11].replace('\\', r'-').replace(r'/', r'-')
        #     if min_date_string is None or date_string >= min_date_string:
        #         if max_date_string is None or date_string <= max_date_string:
        #             all_filenames.append(os.path.join(path_to_weekly_v2_pt1, filename))
        #             all_date_strings.append(date_string)
        path_to_weekly_v2_pt2 = os.path.join(CURRENT_DATA_DIR, '/media/gpu/easystore/Safegraph/Weekly Places Patterns (for data from 2020-11-30 to Present)/home_panel_summary/')
        for year in os.listdir(path_to_weekly_v2_pt2):
            for month in os.listdir(os.path.join(path_to_weekly_v2_pt2, year)):
                for week in os.listdir(os.path.join(path_to_weekly_v2_pt2, '%s/%s/' % (year, month))):
                    for hour in os.listdir(os.path.join(path_to_weekly_v2_pt2, '%s/%s/%s/' % (year, month, week))):
                        date_string = '%s-%s-%s' % (year, month, week)
                        if min_date_string is None or date_string >= min_date_string:
                            if max_date_string is None or date_string <= max_date_string:
                                all_filenames.append(os.path.join(path_to_weekly_v2_pt2, '%s/%s/%s/%s/home_panel_summary.csv' % (year, month, week, hour)))
                                all_date_strings.append(date_string)
    
    files_and_dates = zip(all_filenames, all_date_strings)
    files_and_dates = sorted(files_and_dates, key=lambda x:x[1])  # sort by date_string
    processed_cnt = 0
    cbgs_with_ratio_above_one = np.array([False for a in range(len(acs_data))])
    for filename, date_string in files_and_dates:
        processed_cnt += 1
        print(f"Processing: {processed_cnt} / {len(files_and_dates)}", filename)
        safegraph_counts = pd.read_csv(filename, dtype={'census_block_group':str}, engine='python')

        if 'iso_country_code' in safegraph_counts.columns:
            safegraph_counts = safegraph_counts[safegraph_counts['iso_country_code'] == 'US']
        safegraph_counts = safegraph_counts[['census_block_group', 'number_devices_residing']]  # blockgroup devices count
        col_name = 'number_devices_residing_%s' % date_string
        safegraph_counts.columns = ['census_block_group', col_name]
        safegraph_counts = safegraph_counts[~safegraph_counts['census_block_group'].astype(str).str.contains("CA:")]  # Do not county Canadian block/POIs.
        # safegraph_counts['census_block_group'] = safegraph_counts['census_block_group'].map(int)
        safegraph_counts['census_block_group'] = safegraph_counts['census_block_group'].astype(str).str.zfill(12)
        #print('safegraph_counts:', safegraph_counts)
        assert len(safegraph_counts['census_block_group'].dropna()) == len(safegraph_counts)
        safegraph_counts = safegraph_counts.drop_duplicates(subset=['census_block_group'], keep=False)

        combined_data = pd.merge(combined_data,  # combined_data is copy of acs_data.
                                 safegraph_counts,
                                 how='left',
                                 validate='one_to_one',
                                 on='census_block_group')
        #print("combined_data['census_block_group']: ", combined_data['census_block_group'])
        missing_data_idxs = pd.isnull(combined_data[col_name])
        combined_data.loc[missing_data_idxs, col_name] = 0
        r, p = pearsonr(combined_data['total_cbg_population'], combined_data[col_name])
        combined_data['ratio'] = combined_data[col_name]/combined_data['total_cbg_population']
        cbgs_with_ratio_above_one = cbgs_with_ratio_above_one | (combined_data['ratio'].values > 1) # mobile devices will not more than population
        combined_data.loc[combined_data['total_cbg_population'] == 0, 'ratio'] = None  # some block groups have zero population, then lead to infinit by /zero.
        
        if verbose:
            print("\n*************")
            print("%s: %i devices read from %i rows" % (
                date_string, safegraph_counts[col_name].sum(), len(safegraph_counts)))
            print("Missing data for %i rows; filling with zeros" % missing_data_idxs.sum())
            print("Ratio of SafeGraph count to Census count")
            print(combined_data['ratio'].describe(percentiles=[.25, .5, .75, .9, .99, .999]))
            print("Correlation between SafeGraph and Census counts: %2.3f" % (r))
    print("Warning: %i CBGs with a ratio greater than 1 in at least one period" % cbgs_with_ratio_above_one.sum())
    del combined_data['ratio']
    combined_data.index = range(len(combined_data))
    # assert len(combined_data.dropna()) == len(combined_data)  # Huan "metropolitan Division Code" has nan.
    return combined_data

def load_and_reconcile_multiple_acs_data():   # Now we use the 2019 5-year ACS instead.
    """
    Because we use Census data from two data sources, load a single dataframe that combines both. 
    """
    # acs_5_year_d = load_dataframe_to_correct_for_population_size(just_load_census_data=True)
    # column_rename = {'total_cbg_population': 'total_cbg_population_2019_5YR'}
    # acs_5_year_d = acs_5_year_d.rename(mapper=column_rename, axis=1)
    # acs_5_year_d['state_name'] = acs_1_year_d['state_code'].map(lambda x:FIPS_CODES_FOR_50_STATES_PLUS_DC[str(x)] if str(x) in FIPS_CODES_FOR_50_STATES_PLUS_DC else np.nan)
    # acs_5_year_d = pd.read_csv(PATH_TO_ACS_5YR_DATA)
    # print('%i rows of 2017 5-year ACS data read' % len(acs_5_year_d))
    # acs_5_year_d['census_block_group'] = acs_5_year_d['GEOID'].map(lambda x:x.split("US")[1]).astype(int)
    # # rename dynamic attributes to indicate that they are from ACS 2017 5-year
    # dynamic_attributes = ['p_black', 'p_white', 'p_asian', 'median_household_income',
    #                       'block_group_area_in_square_miles', 'people_per_mile']
    # column_rename = {attr:'%s_2019_5YR' % attr for attr in dynamic_attributes}
    # acs_5_year_d = acs_5_year_d.rename(mapper=column_rename, axis=1)
    # # repetitive with 'state_code' and 'county_code' column from acs_1_year_d
    # acs_5_year_d = acs_5_year_d.drop(['Unnamed: 0', 'STATEFP', 'COUNTYFP'], axis=1)
    # combined_d = pd.merge(acs_1_year_d, acs_5_year_d, on='census_block_group', how='outer', validate='one_to_one')
    # combined_d['people_per_mile_hybrid'] = combined_d['total_cbg_population_2018_1YR'] / combined_d['block_group_area_in_square_miles_2017_5YR']
    # return combined_d
    acs_5_year_d = pd.read_csv(PATH_TO_ACS_5YR_DATA)
    column_rename = {'cbg_total_population': 'total_cbg_population_2019_5YR'}
    acs_5_year_d['GEOID'] = acs_5_year_d['GEOID'].astype(str).str.zfill(12)
    acs_5_year_d['FIPS State Code'] = acs_5_year_d['FIPS State Code'].astype(str).str.zfill(2)
    acs_5_year_d['state_code'] = acs_5_year_d['FIPS State Code'].astype(str).str.zfill(2)
    acs_5_year_d = acs_5_year_d.rename(columns=column_rename)
    return acs_5_year_d



def compute_cbg_day_prop_out(sdm_of_interest, cbgs_of_interest=None):
    '''
    Computes the proportion of people leaving a CBG on each day.
    It returns a new DataFrame, with one row per CBG representing proportions for each day in sdm_of_interest.

    sdm_of_interest: a Social Distancing Metrics dataframe, data for the time period of interest
    cbgs_of_interest: a list, the CBGs for which to compute reweighting; if None, then
                      reweighting is computed for all CBGs in sdm_of_interest

    ---------------------------------------
    Sample usage:

    sdm_sq = helper.load_social_distancing_metrics(status_quo_days)
    days_of_interest = helper.list_datetimes_in_range(datetime.datetime(2020, 3, 1), datetime.datetime(2020, 4, 1))
    sdm_of_interest = helper.load_social_distancing_metrics(days_of_interest)
    reweightings_df = helper.compute_cbg_day_reweighting( sdm_of_interest)

    '''
    # Process SDM of interest dataframe
    orig_len = len(sdm_of_interest)
    interest_num_home_cols = [col for col in sdm_of_interest.columns if col.endswith('_completely_home_device_count')]
    interest_device_count_cols = [col for col in sdm_of_interest.columns if col.endswith('_device_count') and col not in interest_num_home_cols]
    assert len(interest_num_home_cols) == len(interest_device_count_cols)
    date_strs = []
    prop_out_per_day = []
    for home_col, device_col in zip(interest_num_home_cols, interest_device_count_cols):
        home_date = home_col.strip('_completely_home_device_count')
        device_date = device_col.strip('_device_count')
        assert home_date == device_date
        date_strs.append(home_date)
        denom = np.clip(sdm_of_interest[device_col].values, 1, None)  # min 1, so we don't divide by 0
        prop_home = sdm_of_interest[home_col].values / denom
        prop_out = np.clip(1 - prop_home, 1e-10, None)  # so that reweighting is not zero
        median = np.nanmedian(prop_out)
        prop_out[np.isnan(prop_out)] = median  # fill with median from day
        prop_out_per_day.append(prop_out)
    prop_out_per_day = np.array(prop_out_per_day).T  # num cbgs x num days
    prop_df = pd.DataFrame(prop_out_per_day, columns=date_strs)
    prop_df['census_block_group'] = sdm_of_interest.index
    return prop_df

def compute_daily_inter_cbg_travel(sdm_df, cbg_pop_sizes, datetimes, max_upweighting_factor=100):
    assert len(cbg_pop_sizes) == len(sdm_df)
    date_strings = []
    inter_cbg_travel = []
    msa_cbgs = set(sdm_df.index)
    for dt in datetimes:
        date_str = '%s.%s.%s' % (dt.year, dt.month, dt.day)
        date_strings.append(date_str)
        destination_cbg_col = '%s_destination_cbgs' % date_str
        destinction_cbgs_as_dicts = sdm_df[destination_cbg_col].fillna('{}').map(lambda x:Counter(cast_keys_to_ints(json.loads(x))))  # map strings to counters.
        unweighted_travel = []
        for cbg_src, outflow in zip(sdm_df.index, destinction_cbgs_as_dicts):  
            visits_to_other_cbgs = 0
            for cbg_tgt, count in outflow.most_common():
                if cbg_tgt in msa_cbgs and cbg_tgt != cbg_src:
                    visits_to_other_cbgs += count
            unweighted_travel.append(visits_to_other_cbgs)
        unweighted_travel = np.array(unweighted_travel)
        
        devices_col = '%s_device_count' % date_str
        num_devices = np.clip(sdm_df[devices_col].values, 1, None)  # min 1
        scaling_factor = cbg_pop_sizes / num_devices
        num_to_clip = np.sum(scaling_factor > max_upweighting_factor)
        scaling_factor = np.clip(scaling_factor, None, max_upweighting_factor)
        median_factor = np.nanmedian(scaling_factor)
        num_is_nan = np.sum(np.isnan(scaling_factor))
        scaling_factor[np.isnan(scaling_factor)] = median_factor
        print('%s: num scaling factors > %d = %d; num nan = %d; median = %.3f' % 
              (date_str, max_upweighting_factor, num_to_clip, num_is_nan, median_factor))
        weighted_travel = unweighted_travel * scaling_factor
        inter_cbg_travel.append(weighted_travel)
    inter_cbg_travel = np.array(inter_cbg_travel).T  # num_cbgs x num_days
    df = pd.DataFrame(inter_cbg_travel, columns=date_strings)
    df = df.set_index(sdm_df.index)
    return df
    
# http://www.healthdata.org/sites/default/files/files/Projects/COVID/briefing_US_20201223.pdf
# https://royalsocietypublishing.org/doi/10.1098/rsos.200909
def get_daily_case_detection_rate(min_datetime=None, max_datetime=None):
    mar = list_datetimes_in_range(datetime.datetime(2020, 3, 1), datetime.datetime(2020, 3, 31))
    mar_rates = np.linspace(0.05, 0.05, len(mar))
    apr_jul = list_datetimes_in_range(datetime.datetime(2020, 4, 1), datetime.datetime(2020, 7, 31))
    apr_jul_rates = np.linspace(0.05, 0.18, len(apr_jul))
    aug_oct = list_datetimes_in_range(datetime.datetime(2020, 8, 1), datetime.datetime(2020, 10, 31))
    aug_oct_rates = np.linspace(0.18, 0.23, len(aug_oct))
    nov_dec = list_datetimes_in_range(datetime.datetime(2020, 11, 1), datetime.datetime(2020, 12, 31))
    nov_dec_rates = np.linspace(0.55, 0.65, len(nov_dec))
    jan_aug_2021 = list_datetimes_in_range(datetime.datetime(2021, 1, 1), datetime.datetime(2021, 5, 1))
    jan_aug_2021_rates = np.linspace(0.65, 0.65, len(jan_aug_2021))
    all_dates = mar + apr_jul + aug_oct + nov_dec + jan_aug_2021
    all_rates = np.concatenate([mar_rates, apr_jul_rates, aug_oct_rates, nov_dec_rates, jan_aug_2021_rates]).reshape(-1)
    assert len(all_dates) == len(all_rates)
    
    if min_datetime is not None:
        assert min_datetime in all_dates
        start = all_dates.index(min_datetime)
        all_dates = all_dates[start:]
        all_rates = all_rates[start:]
    if max_datetime is not None:
        if max_datetime in all_dates:
            end = all_dates.index(max_datetime)
            all_dates = all_dates[:end+1]
            all_rates = all_rates[:end+1]
        elif max_datetime > all_dates[-1]:
            additional_dates = list_datetimes_in_range(all_dates[-1] + datetime.timedelta(days=1), max_datetime)
            all_dates += additional_dates
            all_rates = list(all_rates)
            for d in additional_dates:
                all_rates.append(all_rates[-1])
            all_rates = np.array(all_rates)
    return all_dates, all_rates
    
def write_out_acs_5_year_data():
    cbg_mapper = CensusBlockGroups(
        base_directory='/media/gpu/easystore/covid_mobility_results/new_census_data/',
        gdb_files=None)

    geometry_cols = ['STATEFP',
              'COUNTYFP',
              'TRACTCE',
              'Metropolitan/Micropolitan Statistical Area',
              'CBSA Title',
              'State Name']
    block_group_cols = ['GEOID',
                        'total_cbg_population',
                        'state_code',
                        'county_code',
                              'p_black',
                              'p_white',
                              'p_asian',
                              'median_household_income',
                             'block_group_area_in_square_miles',
                             'people_per_mile']
    for k in geometry_cols:
        cbg_mapper.block_group_d[k] = cbg_mapper.geometry_d[k].values
    df_to_write_out = cbg_mapper.block_group_d[block_group_cols + geometry_cols]
    print("Total rows: %i" % len(df_to_write_out))
    print("Missing data")
    print(pd.isnull(df_to_write_out).mean())
    df_to_write_out.to_csv(PATH_TO_ACS_5YR_DATA)
    
def load_poi_ids_for_msa(msa_name, ipf_id_prefix=IPF_ID_PREFIX, verbose=True):
    path_to_poi_csv = os.path.join(PATH_TO_NEW_IPF_OUTPUT, '%s/%s_poi_ids.csv' % (msa_name, ipf_id_prefix))
    if verbose:
        print('Loading POI ids from', path_to_poi_csv)
    poi_ids = pd.read_csv(path_to_poi_csv)['poi_id'].values
    return poi_ids
    
def load_cbg_ids_for_msa(msa_name, ipf_id_prefix=IPF_ID_PREFIX, verbose=True):
    path_to_cbg_csv = os.path.join(PATH_TO_NEW_IPF_OUTPUT, '%s/%s_cbg_ids.csv' % (msa_name, ipf_id_prefix))
    if verbose:
        print('Loading CBG ids from', path_to_cbg_csv)
    cbg_ids = pd.read_csv(path_to_cbg_csv)['cbg_id'].values
    return cbg_ids
    
def load_ipf_output_for_hours(msa_name, min_datetime, max_datetime, ipf_id_prefix=IPF_ID_PREFIX, return_ids=True):
    """
    Loads the POI-CBG networks for this MSA and these hours.
    """
    ts = time.time()
    all_hours = list_hours_in_range(min_datetime, max_datetime)
    print('Loading IPF output for %d hours' % len(all_hours))
    poi_cbg_visits_list = []
    for hr in all_hours:
        path_to_npz = os.path.join(PATH_TO_NEW_IPF_OUTPUT, '%s/%s.npz' % (msa_name, hr.strftime('%Y-%m-%d-%H')))
        assert os.path.isfile(path_to_npz), 'File does not exist: %s' % path_to_npz
        try:
            poi_cbg_visits_list.append(scipy.sparse.load_npz(path_to_npz))
        except:
            raise Exception('Failed to load %s' % path_to_npz)
    diff = time.time() - ts
    print('Finished loading IPF output in %.2fs [%.2fs per hour]' % (diff, diff / len(all_hours)))
    if return_ids:
        poi_ids = load_poi_ids_for_msa(msa_name, ipf_id_prefix=ipf_id_prefix)
        cbg_ids = load_cbg_ids_for_msa(msa_name, ipf_id_prefix=ipf_id_prefix)
        assert poi_cbg_visits_list[0].shape == (len(poi_ids), len(cbg_ids))
        return poi_cbg_visits_list, poi_ids, cbg_ids
    return poi_cbg_visits_list

def load_all_poi_visits_for_msa(msa_name, return_ids=True):
    ts = time.time()
    path_to_poi_visits = os.path.join(PATH_TO_NEW_IPF_OUTPUT, '%s/%s_poi_hourly_visits.npy' % (msa_name, POI_VISITS_PREFIX))
    poi_visits = np.load(path_to_poi_visits)
    diff = time.time()
    print('Finished loading all POI visits:', poi_visits.shape)
    if return_ids:
        poi_ids = load_poi_ids_for_msa(msa_name)
        min_hour, max_hour = POI_VISITS_PREFIX.split('_to_')
        year, month, date = min_hour.split('-')
        min_datetime = datetime.datetime(int(year), int(month), int(date), 0)
        year, month, date = max_hour.split('-')
        max_datetime = datetime.datetime(int(year), int(month), int(date), 23)
        all_hours = list_hours_in_range(min_datetime, max_datetime)
        assert poi_visits.shape == (len(poi_ids), len(all_hours))
        return poi_visits, poi_ids, all_hours
    return poi_visits
    
def load_mask_use_data(state_code):
    filepath = os.path.join(PATH_TO_MASK_USE_DATA, '%s.csv' % state_code)
    assert os.path.isfile(filepath), '%s does not exist' % filepath
    df = pd.read_csv(filepath)
    df = df.dropna()
    return df
    
class CensusBlockGroups:
    """
    A class for loading geographic and demographic data from the ACS.

    A census block group is a relatively small area.
    Less good than houses but still pretty granular. https://en.wikipedia.org/wiki/Census_block_group

    Data was downloaded from https://www.census.gov/geographies/mapping-files/time-series/geo/tiger-data.html
    We use the most recent ACS 5-year estimates: 2013-2017, eg:
    wget https://www2.census.gov/geo/tiger/TIGER_DP/2017ACS/ACS_2017_5YR_BG.gdb.zip
    These files are convenient because they combine both geographic boundaries + demographic data, leading to a cleaner join.

    The main method for data access is get_demographic_stats_of_point. Sample usage:
    x = CensusBlockGroups(gdb_files=['ACS_2017_5YR_BG_51_VIRGINIA.gdb'])
    x.get_demographic_stats_of_points(latitudes=[38.8816], longitudes=[-77.0910], desired_cols=['p_black', 'p_white', 'mean_household_income'])
    """
    def __init__(self, base_directory=PATH_TO_CENSUS_BLOCK_GROUP_DATA,
        gdb_files=None,
        county_to_msa_mapping_filepath=PATH_TO_COUNTY_TO_MSA_MAPPING):
        self.base_directory = base_directory
        if gdb_files is None:
            self.gdb_files = ['ACS_2019_5YR_BG.gdb']
        else:
            self.gdb_files = gdb_files
        self.crs_to_use = WGS_84_CRS # https://epsg.io/4326, WGS84 - World Geodetic System 1984, used in GPS.
        self.county_to_msa_mapping_filepath = county_to_msa_mapping_filepath
        self.load_raw_dataframes() # Load in raw geometry and demographic dataframes.

        # annotate demographic data with more useful columns.
        self.annotate_with_race()
        self.annotate_with_income()
        self.annotate_with_counties_to_msa_mapping()
        self.annotate_with_area_and_pop_density()

    def annotate_with_area_and_pop_density(self):
        # https://gis.stackexchange.com/questions/218450/getting-polygon-areas-using-geopandas. 
        # See comments about using cea projection. 
        gdf = self.geometry_d[['geometry']].copy().to_crs({'proj':'cea'})
        area_in_square_meters = gdf['geometry'].area.values
        self.block_group_d['block_group_area_in_square_miles'] = area_in_square_meters / (1609.34 ** 2)
        self.block_group_d['people_per_mile'] = (self.block_group_d['B01001e1'] /
                                               self.block_group_d['block_group_area_in_square_miles'])

        self.block_group_d['total_cbg_population'] = (self.block_group_d['B01001e1'])

        print(self.block_group_d[['block_group_area_in_square_miles', 'people_per_mile']].describe())


    def annotate_with_race(self):
        """
        Analysis focuses on black and non-white population groups. Also annotate with p_asian because of possible anti-Asian discrimination. 
        B03002e1  HISPANIC OR LATINO ORIGIN BY RACE: Total: Total population -- (Estimate)
        B03002e3  HISPANIC OR LATINO ORIGIN BY RACE: Not Hispanic or Latino: White alone: Total population -- (Estimate)
        B03002e4  HISPANIC OR LATINO ORIGIN BY RACE: Not Hispanic or Latino: Black or African American alone: Total population -- (Estimate)
        B03002e6  HISPANIC OR LATINO ORIGIN BY RACE: Not Hispanic or Latino: Asian alone: Total population -- (Estimate)
        """
        print("annotating with race")
        self.block_group_d['p_black'] = self.block_group_d['B03002e4'] / self.block_group_d['B03002e1']
        self.block_group_d['p_white'] = self.block_group_d['B03002e3'] / self.block_group_d['B03002e1']
        self.block_group_d['p_asian'] = self.block_group_d['B03002e6'] / self.block_group_d['B03002e1']
        print(self.block_group_d[['p_black', 'p_white', 'p_asian']].describe())

    def load_raw_dataframes(self):
        """
        Read in the original demographic + geographic data.
        """
        self.block_group_d = None
        self.geometry_d = None
        demographic_layer_names = ['X25_HOUSING_CHARACTERISTICS', 'X01_AGE_AND_SEX', 'X03_HISPANIC_OR_LATINO_ORIGIN', 'X19_INCOME']
        for file in self.gdb_files:
            # https://www.reddit.com/r/gis/comments/775imb/accessing_a_gdb_without_esri_arcgis/doj9zza
            full_path = os.path.join(self.base_directory, file)
            layer_list = fiona.listlayers(full_path)
            print("file:", file)
            print("layer_list:", layer_list)
            geographic_layer_name = [a for a in layer_list if a[:15] == 'ACS_2019_5YR_BG']
            assert len(geographic_layer_name) == 1
            geographic_layer_name = geographic_layer_name[0]

            geographic_data = geopandas.read_file(full_path, layer=geographic_layer_name).to_crs(self.crs_to_use)
            # by default when you use the read file command, the column containing spatial objects is named "geometry", and will be set as the active column.
            print(geographic_data.columns)
            geographic_data = geographic_data.sort_values(by='GEOID_Data')[['GEOID_Data', 'geometry', 'STATEFP', 'COUNTYFP', 'TRACTCE']]
            for demographic_idx, demographic_layer_name in enumerate(demographic_layer_names):
                assert demographic_layer_name in layer_list
                if demographic_idx == 0:
                    demographic_data = geopandas.read_file(full_path, layer=demographic_layer_name)
                else:
                    old_len = len(demographic_data)
                    new_df = geopandas.read_file(full_path, layer=demographic_layer_name)
                    assert sorted(new_df['GEOID']) == sorted(demographic_data['GEOID'])
                    demographic_data = demographic_data.merge(new_df, on='GEOID', how='inner')
                    assert old_len == len(demographic_data)
            demographic_data = demographic_data.sort_values(by='GEOID')

            shared_geoids = set(demographic_data['GEOID'].values).intersection(set(geographic_data['GEOID_Data'].values))
            print("Length of demographic data: %i; geographic data %i; %i GEOIDs in both" % (len(demographic_data), len(geographic_data), len(shared_geoids)))

            demographic_data = demographic_data.loc[demographic_data['GEOID'].map(lambda x:x in shared_geoids)]
            geographic_data = geographic_data.loc[geographic_data['GEOID_Data'].map(lambda x:x in shared_geoids)]

            demographic_data.index = range(len(demographic_data))
            geographic_data.index = range(len(geographic_data))

            assert (geographic_data['GEOID_Data'] == demographic_data['GEOID']).all()
            assert len(geographic_data) == len(set(geographic_data['GEOID_Data']))


            if self.block_group_d is None:
                self.block_group_d = demographic_data
            else:
                self.block_group_d = pd.concat([self.block_group_d, demographic_data])

            if self.geometry_d is None:
                self.geometry_d = geographic_data
            else:
                self.geometry_d = pd.concat([self.geometry_d, geographic_data])

        assert pd.isnull(self.geometry_d['STATEFP']).sum() == 0
        good_idxs = self.geometry_d['STATEFP'].map(lambda x:x in FIPS_CODES_FOR_50_STATES_PLUS_DC).values
        print("Warning: the following State FIPS codes are being filtered out")
        print(self.geometry_d.loc[~good_idxs, 'STATEFP'].value_counts())
        print("%i/%i Census Block Groups in total removed" % ((~good_idxs).sum(), len(good_idxs)))
        self.geometry_d = self.geometry_d.loc[good_idxs]
        self.block_group_d = self.block_group_d.loc[good_idxs]
        self.geometry_d.index = self.geometry_d['GEOID_Data'].values
        self.block_group_d.index = self.block_group_d['GEOID'].values

    def annotate_with_income(self):
        """
        We want a single income number for each block group. This method computes that.
        """
        print("Computing household income")
        # copy-pasted column definitions right out of the codebook.
        codebook_string = """
        B19001e2    HOUSEHOLD INCOME IN THE PAST 12 MONTHS (IN 2017 INFLATION-ADJUSTED DOLLARS): Less than $10,000: Households -- (Estimate)
        B19001e3    HOUSEHOLD INCOME IN THE PAST 12 MONTHS (IN 2017 INFLATION-ADJUSTED DOLLARS): $10,000 to $14,999: Households -- (Estimate)
        B19001e4    HOUSEHOLD INCOME IN THE PAST 12 MONTHS (IN 2017 INFLATION-ADJUSTED DOLLARS): $15,000 to $19,999: Households -- (Estimate)
        B19001e5    HOUSEHOLD INCOME IN THE PAST 12 MONTHS (IN 2017 INFLATION-ADJUSTED DOLLARS): $20,000 to $24,999: Households -- (Estimate)
        B19001e6    HOUSEHOLD INCOME IN THE PAST 12 MONTHS (IN 2017 INFLATION-ADJUSTED DOLLARS): $25,000 to $29,999: Households -- (Estimate)
        B19001e7    HOUSEHOLD INCOME IN THE PAST 12 MONTHS (IN 2017 INFLATION-ADJUSTED DOLLARS): $30,000 to $34,999: Households -- (Estimate)
        B19001e8    HOUSEHOLD INCOME IN THE PAST 12 MONTHS (IN 2017 INFLATION-ADJUSTED DOLLARS): $35,000 to $39,999: Households -- (Estimate)
        B19001e9    HOUSEHOLD INCOME IN THE PAST 12 MONTHS (IN 2017 INFLATION-ADJUSTED DOLLARS): $40,000 to $44,999: Households -- (Estimate)
        B19001e10   HOUSEHOLD INCOME IN THE PAST 12 MONTHS (IN 2017 INFLATION-ADJUSTED DOLLARS): $45,000 to $49,999: Households -- (Estimate)
        B19001e11   HOUSEHOLD INCOME IN THE PAST 12 MONTHS (IN 2017 INFLATION-ADJUSTED DOLLARS): $50,000 to $59,999: Households -- (Estimate)
        B19001e12   HOUSEHOLD INCOME IN THE PAST 12 MONTHS (IN 2017 INFLATION-ADJUSTED DOLLARS): $60,000 to $74,999: Households -- (Estimate)
        B19001e13   HOUSEHOLD INCOME IN THE PAST 12 MONTHS (IN 2017 INFLATION-ADJUSTED DOLLARS): $75,000 to $99,999: Households -- (Estimate)
        B19001e14   HOUSEHOLD INCOME IN THE PAST 12 MONTHS (IN 2017 INFLATION-ADJUSTED DOLLARS): $100,000 to $124,999: Households -- (Estimate)
        B19001e15   HOUSEHOLD INCOME IN THE PAST 12 MONTHS (IN 2017 INFLATION-ADJUSTED DOLLARS): $125,000 to $149,999: Households -- (Estimate)
        B19001e16   HOUSEHOLD INCOME IN THE PAST 12 MONTHS (IN 2017 INFLATION-ADJUSTED DOLLARS): $150,000 to $199,999: Households -- (Estimate)
        B19001e17   HOUSEHOLD INCOME IN THE PAST 12 MONTHS (IN 2017 INFLATION-ADJUSTED DOLLARS): $200,000 or more: Households -- (Estimate)
        """
        self.income_bin_edges = [0] + list(range(10000, 50000, 5000)) + [50000, 60000, 75000, 100000, 125000, 150000, 200000]

        income_column_names_to_vals = {}
        column_codes = codebook_string.split('\n')
        for f in column_codes:
            if len(f.strip()) == 0:
                continue
            col_name = f.split('HOUSEHOLD INCOME')[0].strip()
            if col_name == 'B19001e2':
                val = 10000
            elif col_name == 'B19001e17':
                val = 200000
            else:
                lower_bound = float(f.split('$')[1].split()[0].replace(',', ''))
                upper_bound = float(f.split('$')[2].split(':')[0].replace(',', ''))
                val = (lower_bound + upper_bound) / 2
            income_column_names_to_vals[col_name] = val
            print("The value for column %s is %2.1f" % (col_name, val))

        # each column gives the count of households with that income. So we need to take a weighted sum to compute the average income.
        self.block_group_d['total_household_income'] = 0.
        self.block_group_d['total_households'] = 0.
        for col in income_column_names_to_vals:
            self.block_group_d['total_household_income'] += self.block_group_d[col] * income_column_names_to_vals[col]
            self.block_group_d['total_households'] += self.block_group_d[col]
        self.block_group_d['mean_household_income'] = 1.*self.block_group_d['total_household_income'] / self.block_group_d['total_households']
        self.block_group_d['median_household_income'] = self.block_group_d['B19013e1'] # MEDIAN HOUSEHOLD INCOME IN THE PAST 12 MONTHS (IN 2017 INFLATION-ADJUSTED DOLLARS): Median household income in the past 12 months (in 2017 inflation-adjusted dollars): Households -- (Estimate)
        assert (self.block_group_d['total_households'] == self.block_group_d['B19001e1']).all() # sanity check: our count should agree with theirs.
        assert (pd.isnull(self.block_group_d['mean_household_income']) == (self.block_group_d['B19001e1'] == 0)).all()
        print("Warning: missing income data for %2.1f%% of census blocks with 0 households" % (pd.isnull(self.block_group_d['mean_household_income']).mean() * 100))
        self.income_column_names_to_vals = income_column_names_to_vals
        assert len(self.income_bin_edges) == len(self.income_column_names_to_vals)
        print(self.block_group_d[['mean_household_income', 'total_households']].describe())

    def annotate_with_counties_to_msa_mapping(self):
        """
        Annotate with metropolitan area info for consistency with Experienced Segregation paper.
        # https://www2.census.gov/programs-surveys/metro-micro/geographies/reference-files/2017/delineation-files/list1.xls
        """
        print("Loading county to MSA mapping")
        self.counties_to_msa_df = pd.read_csv(self.county_to_msa_mapping_filepath, skiprows=0, dtype={'FIPS State Code':str, 'FIPS County Code':str})
        print("%i rows read" % len(self.counties_to_msa_df))
        self.counties_to_msa_df = self.counties_to_msa_df[['CBSA Title',
                                                           'Metropolitan/Micropolitan Statistical Area',
                                                           'State Name',
                                                           'FIPS State Code',
                                                           'FIPS County Code']]

        self.counties_to_msa_df.columns = ['CBSA Title',
                                           'Metropolitan/Micropolitan Statistical Area',
                                           'State Name',
                                           'STATEFP',
                                           'COUNTYFP']

        self.counties_to_msa_df = self.counties_to_msa_df.dropna(how='all') # remove a couple blank rows.
        assert self.counties_to_msa_df['Metropolitan/Micropolitan Statistical Area'].map(lambda x:x in ['Metropolitan Statistical Area', 'Micropolitan Statistical Area']).all()
        print("Number of unique Metropolitan statistical areas: %i" %
            len(set(self.counties_to_msa_df.loc[self.counties_to_msa_df['Metropolitan/Micropolitan Statistical Area'] == 'Metropolitan Statistical Area', 'CBSA Title'])))
        print("Number of unique Micropolitan statistical areas: %i" %
            len(set(self.counties_to_msa_df.loc[self.counties_to_msa_df['Metropolitan/Micropolitan Statistical Area'] == 'Micropolitan Statistical Area', 'CBSA Title'])))
        old_len = len(self.geometry_d)
        assert len(self.counties_to_msa_df.drop_duplicates(['STATEFP', 'COUNTYFP'])) == len(self.counties_to_msa_df)


        self.geometry_d = self.geometry_d.merge(self.counties_to_msa_df,
                                                on=['STATEFP', 'COUNTYFP'],
                                                how='left')
        # For some reason the index gets reset here. Annoying, not sure why.
        self.geometry_d.index = self.geometry_d['GEOID_Data'].values

        assert len(self.geometry_d) == old_len
        assert (self.geometry_d.index == self.block_group_d.index).all()

    def get_demographic_stats_of_points(self, latitudes, longitudes, desired_cols):
        """
        Given a list or array of latitudes and longitudes, matches to Census Block Group.
        Returns a dictionary which includes the state and county FIPS code, along with any columns in desired_cols.

        This method assumes the latitudes and longitudes are in https://epsg.io/4326, which is what I think is used for Android/iOS -> SafeGraph coordinates.
        """
        def dtype_pandas_series(obj):
            return str(type(obj)) == "<class 'pandas.core.series.Series'>"
        assert not dtype_pandas_series(latitudes)
        assert not  dtype_pandas_series(longitudes)
        assert len(latitudes) == len(longitudes)

        t0 = time.time()

        # we have to match stuff a million rows at a time because otherwise we get weird memory warnings.
        start_idx = 0
        end_idx = start_idx + int(1e6)
        merged = []
        while start_idx < len(longitudes):
            print("Doing spatial join on points with indices from %i-%i" % (start_idx, min(end_idx, len(longitudes))))

            points = geopandas.GeoDataFrame(pd.DataFrame({'placeholder':np.array(range(start_idx, min(end_idx, len(longitudes))))}), # this column doesn't matter. We just have to create a geo data frame.
                geometry=geopandas.points_from_xy(longitudes[start_idx:end_idx], latitudes[start_idx:end_idx]),
                crs=self.crs_to_use)
            # see eg gdf = geopandas.GeoDataFrame(df, geometry=geopandas.points_from_xy(df.Longitude, df.Latitude)). http://geopandas.org/gallery/create_geopandas_from_pandas.html
            merged.append(sjoin(points, self.geometry_d[['geometry']], how='left', op='within'))
            assert len(merged[-1]) == len(points)
            start_idx += int(1e6)
            end_idx += int(1e6)
        merged = pd.concat(merged)
        merged.index = range(len(merged))
        assert list(merged.index) == list(merged['placeholder'])

        could_not_match = pd.isnull(merged['index_right']).values
        print("Cannot match to a CBG for a fraction %2.3f of points" % could_not_match.mean())

        results = {}
        for k in desired_cols + ['state_fips_code', 'county_fips_code', 'Metropolitan/Micropolitan Statistical Area', 'CBSA Title', 'GEOID_Data', 'TRACTCE']:
            results[k] = [None] * len(latitudes)
        results = pd.DataFrame(results)
        matched_geoids = merged['index_right'].values[~could_not_match]
        for c in desired_cols:
            results.loc[~could_not_match, c] = self.block_group_d.loc[matched_geoids, c].values
            if c in ['p_white', 'p_black', 'mean_household_income', 'median_household_income', 'new_census_monthly_rent_to_annual_income_multiplier', 'new_census_median_monthly_rent_to_annual_income_multiplier']:
                results[c] = results[c].astype('float')

        results.loc[~could_not_match, 'state_fips_code'] = self.geometry_d.loc[matched_geoids, 'STATEFP'].values
        results.loc[~could_not_match, 'county_fips_code'] = self.geometry_d.loc[matched_geoids, 'COUNTYFP'].values
        results.loc[~could_not_match, 'Metropolitan/Micropolitan Statistical Area'] = self.geometry_d.loc[matched_geoids,'Metropolitan/Micropolitan Statistical Area'].values
        results.loc[~could_not_match, 'CBSA Title'] = self.geometry_d.loc[matched_geoids, 'CBSA Title'].values
        results.loc[~could_not_match, 'GEOID_Data'] = self.geometry_d.loc[matched_geoids, 'GEOID_Data'].values
        results.loc[~could_not_match, 'TRACTCE'] = self.geometry_d.loc[matched_geoids, 'TRACTCE'].values

        print("Total query time is %2.3f" % (time.time() - t0))
        return results

if __name__ == "__main__":
    # #write_out_acs_5_year_data()
    # all_dates, all_rates = helper.get_daily_case_detection_rate()  # based on IHME estimate
    # fig, ax = plt.subplots(figsize=(20, 10))
    # ax.plot_date(all_dates, all_rates, marker='.', markersize=5, linestyle='-')
    # ax.xaxis.set_major_locator(mdates.MonthLocator())
    # ax.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d'))
    # ax.set_ylabel('Case detection rate', fontsize=14)
    # ax.tick_params(labelsize=10)
    # ax.grid(alpha=0.3)
    # plt.show()

    load_weekly_patterns_v2_data('2021-06-30')