"""
Estimate glacier snow cover in Sentinel-2, Landsat 8/9, and/or PlanetScope imagery: full pipeline
Rainey Aberle
Department of Geosciences, Boise State University
2023

Requirements:
- Area of Interest (AOI) shapefile: where snow will be classified in all available images.
- Google Earth Engine (GEE) account: used to pull dem over the AOI.
                                     Sign up for a free account [here](https://earthengine.google.com/new_signup/).
- (Optional) Digital elevation model (dem): used to extract elevations over the AOI and for each snowline.
             If no dem is provided, the ASTER Global dem will be loaded through GEE.
- (Optional) Pre-downloaded PlanetScope images.Download images using Planet Explorer (planet.com/explorer) or
             snow-cover-mapping/notebooks/download_PlanetScope_images.ipynb.

Outline:
    0. Setup paths in directory, file locations, authenticate GEE
    1. Sentinel-2 Top of Atmosphere (TOA) imagery: full pipeline
    2. Sentinel-2 Surface Reflectance (SR) imagery: full pipeline
    3. Landsat 8/9 Surface Reflectance (SR) imagery: full pipeline
    4. PlanetScope Surface Reflectance (SR) imagery: full pipeline
"""
# ----------------- #
# --- 0. Set up --- #
# ----------------- #

# -----Import packages
import xarray as xr
import os
import numpy as np
import glob
import geopandas as gpd
import sys
import ee
import json
from tqdm.auto import tqdm
from joblib import load
import argparse
import dask.bag as db
from dask.diagnostics import ProgressBar
import warnings
warnings.simplefilter("ignore")

# -----Parse user arguments
parser = argparse.ArgumentParser(description="snow_classification_pipeline with arguments passed by the user",
                                 formatter_class=argparse.ArgumentDefaultsHelpFormatter)
parser.add_argument('-site_name', default=None, type=str, help='Name of study site')
parser.add_argument('-base_path', default=None, type=str, help='Path in directory to "snow-cover-mapping/"')
parser.add_argument('-aoi_path', default=None, type=str, help='Path in directory to area of interest shapefile')
parser.add_argument('-aoi_fn', default=None, type=str, help='Area of interest file name (.shp)')
parser.add_argument('-dem_path', default=None, type=str, help='(Optional) Path in directory to digital elevation model')
parser.add_argument('-dem_fn', default=None, type=str, help='(Optional) Digital elevation model file name (.shp)')
parser.add_argument('-out_path', default=None, type=str, help='Path in directory where output images will be saved')
parser.add_argument('-ps_im_path', default=None, type=str, help='Path in directory where PlanetScope raw images '
                                                                'are located')
parser.add_argument('-figures_out_path', default=None, type=str, help='Path in directory where figures will be saved')
parser.add_argument('-date_start', default=None, type=str, help='Start date for image querying: "YYYY-MM-DD"')
parser.add_argument('-date_end', default=None, type=str, help='End date for image querying: "YYYY-MM-DD"')
parser.add_argument('-month_start', default=None, type=int, help='Start month for image querying, e.g. 5')
parser.add_argument('-month_end', default=None, type=int, help='End month for image querying, e.g. 10')
parser.add_argument('-cloud_cover_max', default=None, type=int, help='Max. cloud cover percentage in images, '
                                                                     'e.g. 50 = 50% maximum cloud coverage')
parser.add_argument('-mask_clouds', default=None, type=bool, help='Whether to mask clouds using the respective cloud '
                                                                  'cover masking product of each dataset')
parser.add_argument('-im_download', default=False, type=bool, help='Whether to download intermediary images. '
                                                                   'If im_download=False, but images over the AOI '
                                                                   'exceed the GEE limit, images must be '
                                                                   'downloaded regardless.')
parser.add_argument('-steps_to_run', default=None, nargs="+", type=int,
                    help='List of steps to be run, e.g. [1, 2, 3]. '
                         '1=Sentinel-2_TOA, 2=Sentinel-2_SR, 3=Landsat, 4=PlanetScope')
parser.add_argument('-verbose', action='store_true',
                    help='Whether to print details for each image at each processing step.')
args = parser.parse_args()

# -----Set user arguments as variables
site_name = args.site_name
base_path = args.base_path
aoi_path = args.aoi_path
aoi_fn = args.aoi_fn
dem_path = args.dem_path
if dem_path == "None":
    dem_path = None
dem_fn = args.dem_fn
if dem_fn == "None":
    dem_fn = None
out_path = args.out_path
ps_im_path = args.ps_im_path
figures_out_path = args.figures_out_path
date_start = args.date_start
date_end = args.date_end
month_start = args.month_start
month_end = args.month_end
cloud_cover_max = args.cloud_cover_max
mask_clouds = args.mask_clouds
im_download = args.im_download
steps_to_run = args.steps_to_run
verbose = args.verbose

# -----Determine image clipping & plotting settings
plot_results = True  # = True to plot figures of results for each image where applicable
skip_clipped = False  # = True to skip images where bands appear "clipped", i.e. max(blue) < 0.8
crop_to_aoi = True  # = True to crop images to AOI before calculating SCA
save_outputs = True  # = True to save SCAs and snowlines to file
save_figures = True  # = True to save output figures to file

print(site_name)
print(' ')

# -----Set paths for output files
s2_toa_im_path = os.path.join(out_path, 'Sentinel-2_TOA')
s2_sr_im_path = os.path.join(out_path, 'Sentinel-2_SR')
l_im_path = os.path.join(out_path, 'Landsat')
ps_im_masked_path = os.path.join(out_path, 'PlanetScope', 'masked')
ps_im_mosaics_path = os.path.join(out_path, 'PlanetScope', 'mosaics')
im_classified_path = os.path.join(out_path, 'classified')
snowlines_path = os.path.join(out_path, 'snowlines')

# -----Add path to functions
sys.path.insert(1, os.path.join(base_path, 'functions'))
import pipeline_utils as f

# -----Load dataset dictionary
dataset_dict_fn = os.path.join(base_path, 'inputs-outputs', 'datasets_characteristics.json')
dataset_dict = json.load(open(dataset_dict_fn))

# -----Authenticate and initialize GEE
ee.Initialize(opt_url='https://earthengine-highvolume.googleapis.com')

# -----Load AOI as gpd.GeoDataFrame
aoi = gpd.read_file(os.path.join(aoi_path, aoi_fn))
# reproject the AOI to WGS84 to solve for the optimal utm zone
aoi_wgs = aoi.to_crs('EPSG:4326')
aoi_wgs_centroid = [aoi_wgs.geometry[0].centroid.xy[0][0],
                    aoi_wgs.geometry[0].centroid.xy[1][0]]
# grab the optimal utm zone EPSG code
epsg_utm = f.convert_wgs_to_utm(aoi_wgs_centroid[0], aoi_wgs_centroid[1])
print('Optimal UTM CRS = EPSG:' + str(epsg_utm))
# reproject AOI to the optimal utm zone
aoi_utm = aoi.to_crs('EPSG:'+epsg_utm)

# -----Load dem as Xarray DataSet
if dem_fn is None:
    # query GEE for dem
    dem = f.query_gee_for_dem(aoi_utm, base_path, site_name, dem_path)
else:
    # load dem as xarray DataSet
    dem = xr.open_dataset(os.path.join(dem_path, dem_fn))
    dem = dem.rename({'band_data': 'elevation'})
    # set no data values to NaN
    dem = xr.where((dem > 1e38) | (dem <= -9999), np.nan, dem)
    # reproject the dem to the optimal utm zone
    dem = dem.rio.reproject('EPSG:'+str(epsg_utm)).rio.write_crs('EPSG:'+str(epsg_utm))


# ------------------------- #
# --- 1. Sentinel-2 TOA --- #
# ------------------------- #
if 1 in steps_to_run:
    print('----------')
    print('Sentinel-2 TOA')
    print('----------')

    # -----Query GEE for imagery (and download to s2_toa_im_path if necessary)
    dataset = 'Sentinel-2_TOA'
    im_list = f.query_gee_for_imagery(dataset_dict, dataset, aoi_utm, date_start, date_end, month_start,
                                      month_end, cloud_cover_max, mask_clouds, s2_toa_im_path, im_download)

    # -----Check whether images were found
    if type(im_list) == str:
        print('No images found to classify, quitting...')
    else:

        # -----Load trained classifier and feature columns
        clf_fn = os.path.join(base_path, 'inputs-outputs', 'Sentinel-2_TOA_classifier_all_sites.joblib')
        clf = load(clf_fn)
        feature_cols_fn = os.path.join(base_path, 'inputs-outputs', 'Sentinel-2_TOA_feature_columns.json')
        feature_cols = json.load(open(feature_cols_fn))

        # -----Apply pipeline to list of images
        # Convert list of images to dask bag
        im_bag = db.from_sequence(im_list)
        # Create processor with appropriate function arguments
        def create_processor(im_xr):
            snowline_df = f.apply_classification_pipeline(im_xr, dataset_dict, dataset, site_name, im_classified_path,
                                                          snowlines_path,
                                                          aoi_utm, dem, epsg_utm, clf, feature_cols, crop_to_aoi,
                                                          figures_out_path,
                                                          plot_results, verbose)
            return snowline_df
        # Apply batch processing
        with ProgressBar():
            # prepare bag for mapping
            im_bag_results = im_bag.map(create_processor)
            im_bag_results.compute()


# ------------------------ #
# --- 2. Sentinel-2 SR --- #
# ------------------------ #
if 2 in steps_to_run:

    print('----------')
    print('Sentinel-2 SR')
    print('----------')

    # -----Query GEE for imagery and download to s2_sr_im_path if necessary
    dataset = 'Sentinel-2_SR'
    im_list = f.query_gee_for_imagery(dataset_dict, dataset, aoi_utm, date_start, date_end, month_start,
                                      month_end, cloud_cover_max, mask_clouds, s2_sr_im_path, im_download)

    # -----Check whether images were found
    if type(im_list) == str:
        print('No images found to classify, quitting...')
    else:

        # -----Load trained classifier and feature columns
        clf_fn = os.path.join(base_path, 'inputs-outputs', 'Sentinel-2_SR_classifier_all_sites.joblib')
        clf = load(clf_fn)
        feature_cols_fn = os.path.join(base_path, 'inputs-outputs', 'Sentinel-2_SR_feature_columns.json')
        feature_cols = json.load(open(feature_cols_fn))

        # -----Apply pipeline to list of images
        # Convert list of images to dask bag
        im_bag = db.from_sequence(im_list)
        # Create processor with appropriate function arguments
        def create_processor(im_xr):
            snowline_df = f.apply_classification_pipeline(im_xr, dataset_dict, dataset, site_name, im_classified_path,
                                                          snowlines_path,
                                                          aoi_utm, dem, epsg_utm, clf, feature_cols, crop_to_aoi,
                                                          figures_out_path,
                                                          plot_results, verbose)
            return snowline_df
        # Apply batch processing
        with ProgressBar():
            # prepare bag for mapping
            im_bag_results = im_bag.map(create_processor)
            im_bag_results.compute()

# ------------------------- #
# --- 3. Landsat 8/9 SR --- #
# ------------------------- #
if 3 in steps_to_run:

    print('----------')
    print('Landsat 8/9 SR')
    print('----------')

    # -----Query GEE for imagery (and download to l_im_path if necessary)
    dataset = 'Landsat'
    im_list = f.query_gee_for_imagery(dataset_dict, dataset, aoi_utm, date_start, date_end, month_start, month_end,
                                      cloud_cover_max, mask_clouds, l_im_path, im_download)

    # -----Check whether images were found
    if type(im_list) == str:
        print('No images found to classify, quitting...')
    else:

        # -----Load trained classifier and feature columns
        clf_fn = os.path.join(base_path, 'inputs-outputs', 'Landsat_classifier_all_sites.joblib')
        clf = load(clf_fn)
        feature_cols_fn = os.path.join(base_path, 'inputs-outputs', 'Landsat_feature_columns.json')
        feature_cols = json.load(open(feature_cols_fn))

        # -----Apply pipeline to list of images
        # Convert list of images to dask bag
        im_bag = db.from_sequence(im_list)
        # Create processor with appropriate function arguments
        def create_processor(im_xr):
            snowline_df = f.apply_classification_pipeline(im_xr, dataset_dict, dataset, site_name, im_classified_path,
                                                          snowlines_path, aoi_utm, dem, epsg_utm, clf, feature_cols,
                                                          crop_to_aoi, figures_out_path, plot_results, verbose)
            return snowline_df
        # Apply batch processing
        with ProgressBar():
            # prepare bag for mapping
            im_bag_results = im_bag.map(create_processor)
            im_bag_results.compute()


# ------------------------- #
# --- 4. PlanetScope SR --- #
# ------------------------- #
if 4 in steps_to_run:

    print('----------')
    print('PlanetScope SR')
    print('----------')

    # -----Read surface reflectance image file names
    if not ps_im_path:
        print('Variable ps_im_path must be specified to run the PlanetScope classification pipeline, exiting...')
    else:

        dataset = 'PlanetScope'

        # -----Read surface reflectance image file names
        os.chdir(ps_im_path)
        im_fns = sorted(glob.glob('*SR*.tif'))

        # ----Mask clouds and cloud shadows in all images
        plot_results = False
        if mask_clouds:
            print('Masking images using cloud bitmask...')
            for im_fn in tqdm(im_fns):
                f.planetscope_mask_image_pixels(ps_im_path, im_fn, ps_im_masked_path, save_outputs, plot_results)
        # read masked image file names
        os.chdir(ps_im_masked_path)
        im_masked_fns = sorted(glob.glob('*_mask.tif'))

        # -----Mosaic images captured within same hour
        print('Mosaicking images captured in the same hour...')
        if mask_clouds:
            f.planetscope_mosaic_images_by_date(ps_im_masked_path, im_masked_fns, ps_im_mosaics_path, aoi_utm)
            print(' ')
        else:
            f.planetscope_mosaic_images_by_date(ps_im_path, im_fns, ps_im_mosaics_path, aoi_utm)
            print(' ')

            # -----Adjust image radiometry
            im_adj_list = []
            # read mosaicked image file names
            os.chdir(ps_im_mosaics_path)
            im_mosaic_fns = sorted(glob.glob('*.tif'))
            # create polygon(s) of the top and bottom 20th percentile elevations within the aoi
            polygons_top, polygons_bottom = f.create_aoi_elev_polys(aoi_utm, dem)
            # loop through images
            for im_mosaic_fn in tqdm(im_mosaic_fns):

                # -----Open image mosaic
                im_da = xr.open_dataset(ps_im_mosaics_path + im_mosaic_fn)
                # determine image date from image mosaic file name
                im_date = im_mosaic_fn[0:4] + '-' + im_mosaic_fn[4:6] + '-' + im_mosaic_fn[6:8] + 'T' + im_mosaic_fn[
                                                                                                        9:11] + ':00:00'
                im_dt = np.datetime64(im_date)
                print(im_date)

                # -----Adjust radiometry
                im_adj, im_adj_method = f.planetscope_adjust_image_radiometry(im_da, im_dt, polygons_top, polygons_bottom,
                                                                              dataset_dict, skip_clipped)
                if type(im_adj) == str:  # skip if there was an error in adjustment
                    continue
                else:
                    im_adj_list.append(im_adj)

            # -----Load trained classifier and feature columns
            clf_fn = os.path.join(base_path, 'inputs-outputs', 'PlanetScope_classifier_all_sites.joblib')
            clf = load(clf_fn)
            feature_cols_fn = os.path.join(base_path, 'inputs-outputs', 'PlanetScope_feature_columns.json')
            feature_cols = json.load(open(feature_cols_fn))

            # -----Apply pipeline to list of images
            # Convert list of images to dask bag
            im_bag = db.from_sequence(im_list)
            # Create processor with appropriate function arguments
            def create_processor(im_xr):
                snowline_df = f.apply_classification_pipeline(im_xr, dataset_dict, dataset, site_name, im_classified_path,
                                                              snowlines_path,
                                                              aoi_utm, dem, epsg_utm, clf, feature_cols, crop_to_aoi,
                                                              figures_out_path,
                                                              plot_results, verbose)
                return snowline_df
            # Apply batch processing
            with ProgressBar():
                # prepare bag for mapping
                im_bag_results = im_bag.map(create_processor)
                im_bag_results.compute()

print('Done!')
