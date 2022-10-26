# Functions for image adjustment and snow classification in PlanetScope 4-band images
# Rainey Aberle
# 2022

import math
import rasterio as rio
from rasterio.mask import mask
import numpy as np
from pyproj import Proj, transform, Transformer
import matplotlib
import matplotlib.pyplot as plt
import subprocess
import os
from shapely.geometry import Polygon, MultiPolygon, shape, Point, LineString
from scipy.interpolate import interp2d, griddata
from scipy.signal import medfilt
from skimage.measure import find_contours
from scipy.ndimage import binary_fill_holes
import glob
import ee
import geopandas as gpd
import pandas as pd
from scipy import stats
import geemap
from osgeo import gdal
import wxee as wx
import xarray as xr
import rioxarray as rxr

# --------------------------------------------------
def plot_im_RGB_histogram(im_path, im_fn):
    '''
    Plot PlanetScope 4-band RGB image with histograms for the B, G, R, and NIR bands.
    
    Parameters
    ----------
    im_path: str
        path in directory to image
        
    im_fn: str
        image file name
    
    Returns
    ----------
    fig: matplotlib.figure
        resulting figure handle
    
    '''
    
    from osgeo import gdal
    
    # load image
    im = rio.open(im_path + im_fn)
    
    # load bands (blue, green, red, near infrared)
    b = im.read(1).astype(float)
    g = im.read(2).astype(float)
    r = im.read(3).astype(float)
    nir = im.read(4).astype(float)
    if np.nanmax(b) > 1e3:
        im_scalar = 10000
        b = b / im_scalar
        g = g / im_scalar
        r = r / im_scalar
        nir = nir / im_scalar
    # replace no data values with NaN
    b[b==0] = np.nan
    g[g==0] = np.nan
    r[r==0] = np.nan
    nir[nir==0] = np.nan
        
    # define coordinates grid
    im_x = np.linspace(im.bounds.left, im.bounds.right, num=np.shape(b)[1])
    im_y = np.linspace(im.bounds.top, im.bounds.bottom, num=np.shape(b)[0])
    
    # plot RGB image and band histograms
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(10,6), gridspec_kw={'height_ratios': [1]})
    plt.rcParams.update({'font.size': 12, 'font.serif': 'Arial'})
    ax1.imshow(np.dstack([r, g, b]), aspect='auto',
               extent=(np.min(im_x)/1000, np.max(im_x/1000), np.min(im_y)/1000, np.max(im_y)/1000))
    ax1.set_xlabel('Easting [km]')
    ax1.set_ylabel('Northing [km]')
    ax2.hist(b.flatten(), color='blue', histtype='step', bins=100, label='blue')
    ax2.hist(g.flatten(), color='green', histtype='step', bins=100, label='green')
    ax2.hist(r.flatten(), color='red', histtype='step', bins=100, label='red')
    ax2.hist(nir.flatten(), color='brown', histtype='step', linewidth=2, bins=100, label='NIR')
    ax2.set_xlabel('Surface reflectance')
    ax2.set_ylabel('Pixel counts')
    ax2.grid()
    ax2.legend(loc='right')
    fig.suptitle(im_fn)
    fig.tight_layout()
    plt.show()
    
    return fig

# --------------------------------------------------
def plot_im_classified_histogram_contour(im, im_classified, DEM, AOI, contour):
    '''
    Plot the classified image with snow elevations histogram and plot an elevation contour corresponding to the estimated snow line elevation.
    
    Parameters
    ----------
    im: xarray.DataArray
        input image
        
    im_classified: xarray.DataArray
        single band, classified image
        
    DEM: xarray.DataSet
        digital elevation model over the image area
        
    AOI: geopandas.geodataframe.GeoDataFrame
        area of interest
        
    contour: float
        elevation to be plotted as a contour on the figure
    
    Returns
    ----------
    fig: matplotlib.figure
        resulting figure handle
        
    ax: matplotlib.Axes
        axes handles on figure
        
    sl_points_AOI:
    '''
    
    # -----Determine snow-covered elevations
    # mask the DEM using the AOI
    mask = rio.features.geometry_mask(AOI.geometry,
                                      out_shape=(len(DEM.y), len(DEM.x)),
                                      transform=DEM.transform,
                                      invert=True)
    mask = xr.DataArray(mask , dims=("y", "x"))
    # mask DEM values outside the AOI
    DEM_AOI = DEM.where(mask == True)
    # interpolate DEM to the image coordinates
    im_classified = im_classified.squeeze(drop=True) # drop uneccesary dimensions
    x, y = im_classified.indexes.values() # grab indices of image
    DEM_AOI_interp = DEM_AOI.interp(x=x, y=y, method="nearest") # interpolate DEM to image coordinates
    # determine snow covered elevations
    DEM_AOI_interp_snow = DEM_AOI_interp.where(im_classified<=2) # mask pixels not classified as snow
    snow_est_elev = DEM_AOI_interp_snow.elevation.data.flatten() # create array of snow-covered pixel elevations
    snow_est_elev = snow_est_elev[~np.isnan(snow_est_elev)] # remove NaN values

    # -----Determine bins to use in histogram
    elev_min = np.fix(np.nanmin(DEM_AOI_interp.elevation.data.flatten())/10)*10
    elev_max = np.round(np.nanmax(DEM_AOI_interp.elevation.data.flatten())/10)*10
    bin_edges = np.linspace(elev_min, elev_max, num=int((elev_max-elev_min)/10 + 1))
    bin_centers = (bin_edges[1:] + bin_edges[0:-1]) / 2

    # -----Calculate elevation histograms
    H_DEM = np.histogram(DEM_AOI_interp.elevation.data.flatten(), bins=bin_edges)[0]
    H_snow_est_elev = np.histogram(snow_est_elev, bins=bin_edges)[0]
    H_snow_est_elev_norm = H_snow_est_elev / H_DEM
        
    # -----Plot
    fig, ax = plt.subplots(2, 2, figsize=(12,8), gridspec_kw={'height_ratios': [3, 1]})
    ax = ax.flatten()
    plt.rcParams.update({'font.size': 14, 'font.sans-serif': 'Arial'})
    # define x and y limits
    xmin, xmax = np.min(im_classified.x.data)/1e3, np.max(im_classified.x.data)/1e3
    ymin, ymax = np.min(im_classified.y.data)/1e3, np.max(im_classified.y.data)/1e3
    # define colors for plotting
    color_snow = '#4eb3d3'
    color_ice = '#084081'
    color_rock = '#fdbb84'
    color_water = '#bdbdbd'
    color_contour = '#f768a1'
    # create colormap
    colors = [color_snow, color_snow, color_ice, color_rock, color_water]
    cmp = matplotlib.colors.ListedColormap(colors)
    # RGB image
    ax[0].imshow(np.dstack([im.data[2], im.data[1], im.data[0]]),
               extent=(xmin, xmax, ymin, ymax))
    ax[0].set_xlabel("Easting [km]")
    ax[0].set_ylabel("Northing [km]")
    # classified image
    ax[1].imshow(im_classified.data, cmap=cmp, vmin=1, vmax=5,
                 extent=(xmin, xmax, ymin, ymax))
    # plot dummy points for legend
    ax[1].scatter(0, 0, color=color_snow, s=50, label='snow')
    ax[1].scatter(0, 0, color=color_ice, s=50, label='ice')
    ax[1].scatter(0, 0, color=color_rock, s=50, label='rock')
    ax[1].scatter(0, 0, color=color_water, s=50, label='water')
    ax[1].set_xlabel('Easting [km]')
    # AOI
    ax[0].plot([x/1e3 for x in AOI.exterior[0].coords.xy[0]], [y/1e3 for y in AOI.exterior[0].coords.xy[1]], '-k', linewidth=1, label='AOI')
    ax[1].plot([x/1e3 for x in AOI.exterior[0].coords.xy[0]], [y/1e3 for y in AOI.exterior[0].coords.xy[1]], '-k', linewidth=1, label='_nolegend_')
    # elevation contour - save only those inside the AOI
    if contour is not None:
        sl = plt.contour(DEM.x.data, DEM.y.data, DEM.elevation.data[0],[contour])
        sl_points_AOI = [] # initialize list of points
        for path in sl.collections[0].get_paths(): # loop through paths
            v = path.vertices
            for pt in v:
                pt_shapely = Point(pt[0], pt[1])
                if AOI.contains(pt_shapely)[0]:
                        sl_points_AOI.append([pt_shapely.xy[0][0], pt_shapely.xy[1][0]])
        ax[0].plot([pt[0]/1e3 for pt in sl_points_AOI], [pt[1]/1e3 for pt in sl_points_AOI], '.', color=color_contour, markersize=3, label='sl$_{estimated}$')
        ax[1].plot([pt[0]/1e3 for pt in sl_points_AOI], [pt[1]/1e3 for pt in sl_points_AOI], '.', color=color_contour, markersize=3, label='_nolegend_')
        ax[1].set_xlabel("Easting [km]")
    else:
        sl_points_AOI = None
    # reset x and y limits
    ax[0].set_xlim(xmin, xmax)
    ax[0].set_ylim(ymin, ymax)
    ax[1].set_xlim(xmin, xmax)
    ax[1].set_ylim(ymin, ymax)
    # image bands histogram
    h_b = ax[2].hist(im.data[0].flatten(), color='blue', histtype='step', linewidth=2, bins=100, label="blue")
    h_g = ax[2].hist(im.data[1].flatten(), color='green', histtype='step', linewidth=2, bins=100, label="green")
    h_r = ax[2].hist(im.data[2].flatten(), color='red', histtype='step', linewidth=2, bins=100, label="red")
    h_nir = ax[2].hist(im.data[3].flatten(), color='brown', histtype='step', linewidth=2, bins=100, label="NIR")
    ax[2].set_xlabel("Surface reflectance")
    ax[2].set_ylabel("Pixel counts")
    ax[2].legend(loc='best')
    ax[2].grid()
    # normalized snow elevations histogram
    ax[3].bar(bin_centers, H_snow_est_elev_norm, width=(bin_centers[1]-bin_centers[0]), color=color_snow, align='center')
    ax[3].set_xlabel("Elevation [m]")
    ax[3].set_ylabel("% snow-covered")
    ax[3].grid()
    ax[3].set_xlim(elev_min-10, elev_max+10)
    ax[3].set_ylim(0,1)
    # contour line
    if contour is not None:
        ax[3].plot((contour, contour), (0, 1), color=color_contour)
    fig.tight_layout()
    
    return fig, ax, sl_points_AOI
        
# --------------------------------------------------
def snow_mask_to_polygons(mask, im_fn, min_area):
    '''
    Convert snow-covered area in classified image to polygons. Adapted from https://rocreguant.com/convert-a-mask-into-a-polygon-for-images-using-shapely-and-rasterio/1786/
    
    Parameters
    ----------
    mask: numpy.array
        binary mask where True/1 = snow, False/0 = no snow
        
    im_fn: str
        classified image file name
        
    min_area: float
        minimum area of polygons. Polygons with an area less than min_area will be removed
    
    Returns
    ----------
    polygons_list_filtered: list
        list of snow-covered Shapely Polygons
    '''
    
    im = rio.open(im_fn)
    
    all_polygons = []
    for s, value in rio.features.shapes(mask.astype(np.int16), mask=(mask >0), transform=im.transform):
        all_polygons.append(shape(s))

    all_polygons = MultiPolygon(all_polygons)
    if not all_polygons.is_valid:
        all_polygons = all_polygons.buffer(0)
        # Sometimes buffer() converts a simple Multipolygon to just a Polygon,
        # need to keep it a Multi throughout
        if all_polygons.type == 'Polygon':
            all_polygons = MultiPolygon([all_polygons])
           
    # create list of polygons
    polygons_list = list(all_polygons.geoms)
    
    # filter polygons by area
    polygons_list_filtered = []
    for p in polygons_list:
        area = p.area
        if area < min_area:
            continue
        else:
            polygons_list_filtered = polygons_list_filtered + [p]
    
    return polygons_list_filtered
    
# --------------------------------------------------
def mosaic_ims_by_date(im_path, im_fns, ext, out_path, AOI, plot_results):
    '''
    Mosaic PlanetScope 4-band images captured within the same hour using gdal_merge.py. Skips images which contain no real data in the AOI. Adapted from code developed by Jukes Liu.
    
    Parameters
    ----------
    im_path: str
        path in directory to input images.
    im_fns: list of strings
        file names of images to be mosaicked, located in im_path.
    ext: str
        image file extensions, e.g. "SR_clip" or "SR_harmonized"
    out_path: str
        path in directory where image mosaics will be saved.
    AOI: geopandas.geodataframe.GeoDataFrame
        area of interest. If no real data exist within the AOI, function will exit. AOI must be in the same CRS as the images.
    plot_results: bool
    
    Returns
    ----------
    N/A
    
    '''
    
    # -----Create output directory if it does not exist
    if os.path.isdir(out_path)==0:
        os.mkdir(out_path)
        print('Created directory for image mosaics: ' + out_path)
    
    # ----Grab all unique scenes (images captured within the same hour)
    unique_scenes = []
    for scene in im_fns:
        date = scene[0:11]
        unique_scenes.append(date)
    unique_scenes = list(set(unique_scenes))
    unique_scenes.sort() # sort chronologically
    
    # -----Loop through unique scenes
    for scene in unique_scenes:
        
        # define the out path with correct extension
        if ext == 'DN_udm.tif':
            out_im_fn = os.path.join(scene + "_DN_mask.tif")
        elif ext == 'udm2.tif':
            out_im_fn = os.path.join(scene + "_mask.tif")
        else:
            out_im_fn = os.path.join(scene + ".tif")
        print(out_im_fn)
            
        # check if image mosaic already exists in directory
        if os.path.exists(out_path + out_im_fn)==True:
            print("image mosaic already exists... skipping.")
            print(" ")
            
            # plot output file
            if plot_results:
                fig = plot_im_RGB_histogram(out_path, out_im_fn)
            
        else:
            
            file_paths = [] # files from the same hour to mosaic together
            for im_fn in im_fns: # check all files
                if (scene in im_fn): # if they match the scene date
                    im = rio.open(im_path + im_fn) # open image
                    AOI_UTM = AOI.to_crs(str(im.crs)[5:]) # reproject AOI to image CRS
                    # mask the image using AOI geometry
                    b = im.read(1).astype(float) # blue band
                    mask = rio.features.geometry_mask(AOI_UTM.geometry,
                                                   b.shape,
                                                   im.transform,
                                                   all_touched=False,
                                                   invert=False)
                    # check if real data values exist within AOI
                    b_AOI = b[mask==0] # grab blue band values within AOI
                    # set no-data values to NaN
                    b_AOI[b_AOI==-9999] = np.nan
                    b_AOI[b_AOI==0] = np.nan
                    if (len(b_AOI[~np.isnan(b_AOI)]) > 0):
                        file_paths.append(im_path + im_fn) # add the path to the file
                        
            # check if any filepaths were added
            if len(file_paths) > 0:

                # construct the gdal_merge command
                cmd = 'gdal_merge.py -v '

                # add input files to command
                for file_path in file_paths:
                    cmd += file_path+' '

                cmd += '-o ' + out_path + out_im_fn

                # run the command
                p = subprocess.run(cmd, shell=True, capture_output=True)
                print(p)
            
                # plot output file
                if plot_results:
                    fig = plot_im_RGB_histogram(out_path, out_im_fn)
            else:
                
                print("No real data values within the AOI for images on this date... skipping.")
                print(" ")

# --------------------------------------------------
def into_range(x, range_min, range_max):
    shiftedx = x - range_min
    delta = range_max - range_min
    return (((shiftedx % delta) + delta) % delta) + range_min
    
# --------------------------------------------------
def sunpos(when, location, refraction):
    '''
    Determine the sun azimuth and elevation using the date and location.
    Modified from: https://levelup.gitconnected.com/python-sun-position-for-solar-energy-and-research-7a4ead801777
    Parameters
    ----------
    when: str array
        date of image capture ('YYYY', 'MM', 'DD', 'hh', 'mm', 'ss')
    location = coordinate pair (floats)
        approximate location of image capture (latitude, longitude)
    refraction: bool
        whether to account for refraction (bool)
    
    Returns
    ----------
    azimuth: float
        sun azimuth in degrees
    elevation: float
        sun elevation in degrees (float)
    '''
    
    # Extract the passed data
    year, month, day, hour, minute, second = when
    latitude, longitude = location

    # Math typing shortcuts
    rad, deg = math.radians, math.degrees
    sin, cos, tan = math.sin, math.cos, math.tan
    asin, atan2 = math.asin, math.atan2

    # Convert latitude and longitude to radians
    rlat = rad(latitude)
    rlon = rad(longitude)

    # Decimal hour of the day at Greenwich
    greenwichtime = hour + minute / 60 + second / 3600

    # Days from J2000, accurate from 1901 to 2099
    daynum = (
        367 * year
        - 7 * (year + (month + 9) // 12) // 4
        + 275 * month // 9
        + day
        - 730531.5
        + greenwichtime / 24
    )

    # Mean longitude of the sun
    mean_long = daynum * 0.01720279239 + 4.894967873

    # Mean anomaly of the Sun
    mean_anom = daynum * 0.01720197034 + 6.240040768

    # Ecliptic longitude of the sun
    eclip_long = (
        mean_long
        + 0.03342305518 * sin(mean_anom)
        + 0.0003490658504 * sin(2 * mean_anom)
    )

    # Obliquity of the ecliptic
    obliquity = 0.4090877234 - 0.000000006981317008 * daynum

    # Right ascension of the sun
    rasc = atan2(cos(obliquity) * sin(eclip_long), cos(eclip_long))

    # Declination of the sun
    decl = asin(sin(obliquity) * sin(eclip_long))

    # Local sidereal time
    sidereal = 4.894961213 + 6.300388099 * daynum + rlon

    # Hour angle of the sun
    hour_ang = sidereal - rasc

    # Local elevation of the sun
    elevation = asin(sin(decl) * sin(rlat) + cos(decl) * cos(rlat) * cos(hour_ang))

    # Local azimuth of the sun
    azimuth = atan2(
        -cos(decl) * cos(rlat) * sin(hour_ang),
        sin(decl) - sin(rlat) * sin(elevation),
    )
    
    # Convert azimuth and elevation to degrees
    azimuth = into_range(deg(azimuth), 0, 360)
    elevation = into_range(deg(elevation), -180, 180)

    # Refraction correction (optional)
    if refraction:
        targ = rad((elevation + (10.3 / (elevation + 5.11))))
        elevation += (1.02 / tan(targ)) / 60

    # Return azimuth and elevation in degrees
    return (round(azimuth, 2), round(elevation, 2))

# --------------------------------------------------
def apply_hillshade_correction(crs, polygon, im, im_name, im_path, DEM_path, hs_path, out_path, skip_clipped, plot_results):
    '''
    Adjust image using by generating a hillshade model and minimizing the standard deviation of each band within the defined SCA
    
    Parameters
    ----------
    crs: float
        Coordinate Reference System (EPSG code)
    polygon:  shapely.geometry.polygon.Polygon
            polygon, where the band standard deviation will be minimized
    im: rasterio object
        input image
    im_name: str
        file name name of the input image
    im_path: str
        path in directory to the input image
    DEM_path: str
        path in directory to the DEM used to generate the hillshade model
    hs_path: str
        path to save hillshade model
    out_path: str
        path to save corrected image file
    skip_clipped: bool
        whether to skip images where bands appear "clipped"
    plot_results: bool
        whether to plot results to a matplotlib.pyplot.figure
    
    Returns
    ----------
    im_corrected_name: str
        file name of the hillshade-corrected image saved to file
    '''

    print('HILLSHADE CORRECTION')

    # -----Read image bands
    im_scalar = 10000
    b = im.read(1).astype(float)
    g = im.read(2).astype(float)
    r = im.read(3).astype(float)
    nir = im.read(4).astype(float)
    # divide by im_scalar if they have not been already
    if (np.nanmean(b)>1e3):
        b = b / im_scalar
        g = g / im_scalar
        r = r / im_scalar
        nir = nir / im_scalar
            
    # -----Return if image bands are likely clipped
    if skip_clipped==True:
        if (np.nanmax(b) < 0.8) or (np.nanmax(g) < 0.8) or (np.nanmax(r) < 0.8):
            print('image bands appear clipped... skipping.')
            im_corrected_name = 'N/A'
            return im_corrected_name
        
    # -----Define coordinates grid
    im_x = np.linspace(im.bounds.left, im.bounds.right, num=np.shape(b)[1])
    im_y = np.linspace(im.bounds.top, im.bounds.bottom, num=np.shape(b)[0])
        
    # -----filter image points outside the SCA
    im_x_mesh, im_y_mesh = np.meshgrid(im_x, im_y)
    b_polygon = b[np.where((im_x_mesh >= polygon.bounds[0]) & (im_x_mesh <= polygon.bounds[2]) &
                      (im_y_mesh >= polygon.bounds[1]) & (im_y_mesh <= polygon.bounds[3]))]
    g_polygon = g[np.where((im_x_mesh >= polygon.bounds[0]) & (im_x_mesh <= polygon.bounds[2]) &
                      (im_y_mesh >= polygon.bounds[1]) & (im_y_mesh <= polygon.bounds[3]))]
    r_polygon = r[np.where((im_x_mesh >= polygon.bounds[0]) & (im_x_mesh <= polygon.bounds[2]) &
                      (im_y_mesh >= polygon.bounds[1]) & (im_y_mesh <= polygon.bounds[3]))]
    nir_polygon = nir[np.where((im_x_mesh >= polygon.bounds[0]) & (im_x_mesh <= polygon.bounds[2]) &
                           (im_y_mesh >= polygon.bounds[1]) & (im_y_mesh <= polygon.bounds[3]))]
                               
    # -----Return if image does not contain real values within the SCA
    if ((np.min(polygon.exterior.xy[0])>np.min(im_x))
        & (np.max(polygon.exterior.xy[0])<np.max(im_x))
        & (np.min(polygon.exterior.xy[1])>np.min(im_y))
        & (np.max(polygon.exterior.xy[1])<np.max(im_y))
        & (np.nanmean(b_polygon)>0))==False:
        
        print('image does not contain real values within the SCA... skipping.')
        im_corrected_name = 'N/A'
        return im_corrected_name
                
    # -----Extract image information for sun position calculation
    # location: grab center image coordinate, convert to lat lon
    xmid = ((im.bounds.right - im.bounds.left)/2 + im.bounds.left)
    ymid = ((im.bounds.top - im.bounds.bottom)/2 + im.bounds.bottom)
    transformer = Transformer.from_crs("epsg:"+str(crs), "epsg:4326")
    location = transformer.transform(xmid, ymid)
    # when: year, month, day, hour, minute, second
    when = (float(im_name[0:4]), float(im_name[4:6]), float(im_name[6:8]),
            float(im_name[9:11]), float(im_name[11:13]), float(im_name[13:15]))
    # sun azimuth and elevation
    azimuth, elevation = sunpos(when, location, refraction=1)

    # -----Make directory for hillshade models (if it does not already exist in file)
    if os.path.exists(hs_path)==False:
        os.mkdir(hs_path)
        print('made directory for hillshade model:'+hs_path)
            
    # -----Create hillshade model (if it does not already exist in file)
    hs_fn = hs_path+str(azimuth)+'-az_'+str(elevation)+'-z_hillshade.tif'
    if os.path.exists(hs_fn):
        print('hillshade model already exists in directory, loading...')
    else:
#                print('creating hillshade model...')
        # construct the gdal_merge command
        # modified from: https://github.com/clhenrick/gdal_hillshade_tutorial
        # gdaldem hillshade -az aximuth -z elevation dem.tif hillshade.tif
        cmd = 'gdaldem hillshade -az '+str(azimuth)+' -z '+str(elevation)+' '+str(DEM_path)+' '+hs_fn
        # run the command
        p = subprocess.run(cmd, shell=True, capture_output=True)
        print(p)

    # -----load hillshade model from file
    hs = rio.open(hs_fn)
#            print('hillshade model loaded from file...')
    # coordinates
    hs_x = np.linspace(hs.bounds.left, hs.bounds.right, num=np.shape(hs.read(1))[1])
    hs_y = np.linspace(hs.bounds.top, hs.bounds.bottom, num=np.shape(hs.read(1))[0])

    # -----Resample hillshade to image coordinates
    # resampled hillshade file name
    hs_resamp_fn = hs_path+str(azimuth)+'-az_'+str(elevation)+'-z_hillshade_resamp.tif'
    # create interpolation object
    f = interp2d(hs_x, hs_y, hs.read(1))
    hs_resamp = f(im_x, im_y)
    hs_resamp = np.flipud(hs_resamp)
    # save to file
    with rio.open(hs_resamp_fn,'w',
                  driver='GTiff',
                  height=hs_resamp.shape[0],
                  width=hs_resamp.shape[1],
                  dtype=hs_resamp.dtype,
                  count=1,
                  crs=im.crs,
                  transform=im.transform) as dst:
        dst.write(hs_resamp, 1)
    print('resampled hillshade model saved to file:',hs_resamp_fn)

    # -----load resampled hillshade model
    hs_resamp = rio.open(hs_resamp_fn).read(1)
    print('resampled hillshade model loaded from file')
    # -----filter hillshade model points outside the SCA
    hs_polygon = hs_resamp[np.where((im_x_mesh >= polygon.bounds[0]) & (im_x_mesh <= polygon.bounds[2]) & (im_y_mesh >= polygon.bounds[1]) & (im_y_mesh <= polygon.bounds[3]))]

    # -----normalize hillshade model
    hs_norm = (hs_resamp - np.min(hs_resamp)) / (np.max(hs_resamp) - np.min(hs_resamp))
    hs_polygon_norm = (hs_polygon - np.min(hs_polygon)) / (np.max(hs_polygon) - np.min(hs_polygon))

            # -----plot resampled, normalized hillshade model for sanity check
    #        fig, (ax1, ax2) = plt.subplots(1,2,figsize=(12,8))
    #        hs_im = ax1.imshow(hs.read(1), extent=(np.min(hs_x)/1000, np.max(hs_x)/1000, np.min(hs_y)/1000, np.max(hs_y)/1000))
    #        hsnorm_im = ax2.imshow(hs_norm, extent=(np.min(im_x)/1000, np.max(im_x)/1000, np.min(im_y)/1000, np.max(im_y)/1000))
    #        ax2.plot([x/1000 for x in SCA.exterior.xy[0]], [y/1000 for y in SCA.exterior.xy[1]], color='white', linewidth=2, label='SCA')
    #        fig.colorbar(hs_im, ax=ax1, shrink=0.5)
    #        fig.colorbar(hsnorm_im, ax=ax2, shrink=0.5)
    #        plt.show()
            
    # -----loop through hillshade scalar multipliers
#            print('solving for optimal band scalars...')
    # define scalars to test
    hs_scalars = np.linspace(0,0.5,num=21)
    # blue
    b_polygon_mu = np.zeros(len(hs_scalars)) # mean
    b_polygon_sigma =np.zeros(len(hs_scalars)) # std
    # green
    g_polygon_mu = np.zeros(len(hs_scalars)) # mean
    g_polygon_sigma = np.zeros(len(hs_scalars)) # std
    # red
    r_polygon_mu = np.zeros(len(hs_scalars)) # mean
    r_polygon_sigma = np.zeros(len(hs_scalars)) # std
    # nir
    nir_polygon_mu = np.zeros(len(hs_scalars)) # mean
    nir_polygon_sigma = np.zeros(len(hs_scalars)) # std
    i=0 # loop counter
    for hs_scalar in hs_scalars:
        # full image
        b_adj = b - (hs_norm * hs_scalar)
        g_adj = g - (hs_norm * hs_scalar)
        r_adj = r - (hs_norm * hs_scalar)
        nir_adj = nir - (hs_norm * hs_scalar)
        # SCA
        b_polygon_mu[i] = np.nanmean(b_polygon- (hs_polygon_norm * hs_scalar))
        b_polygon_sigma[i] = np.nanstd(b_polygon- (hs_polygon_norm * hs_scalar))
        g_polygon_mu[i] = np.nanmean(g_polygon- (hs_polygon_norm * hs_scalar))
        g_polygon_sigma[i] = np.nanstd(g_polygon- (hs_polygon_norm * hs_scalar))
        r_polygon_mu[i] = np.nanmean(r_polygon- (hs_polygon_norm * hs_scalar))
        r_polygon_sigma[i] = np.nanstd(r_polygon- (hs_polygon_norm * hs_scalar))
        nir_polygon_mu[i] = np.nanmean(nir_polygon- (hs_polygon_norm * hs_scalar))
        nir_polygon_sigma[i] = np.nanstd(nir_polygon- (hs_polygon_norm * hs_scalar))
        i+=1 # increase loop counter

    # -----Determine optimal scalar for each image band
    Ib = np.where(b_polygon_sigma==np.min(b_polygon_sigma))[0][0]
    b_scalar = hs_scalars[Ib]
    Ig = np.where(g_polygon_sigma==np.min(g_polygon_sigma))[0][0]
    g_scalar = hs_scalars[Ig]
    Ir = np.where(r_polygon_sigma==np.min(r_polygon_sigma))[0][0]
    r_scalar = hs_scalars[Ir]
    Inir = np.where(nir_polygon_sigma==np.min(nir_polygon_sigma))[0][0]
    nir_scalar = hs_scalars[Inir]
    print('Optimal scalars:  Blue   |   Green   |   Red   |   NIR')
    print(b_scalar, g_scalar, r_scalar, nir_scalar)

    # -----Apply optimal hillshade model correction
    b_corrected = b - (hs_norm * hs_scalars[Ib])
    g_corrected = g - (hs_norm * hs_scalars[Ig])
    r_corrected = r - (hs_norm * hs_scalars[Ir])
    nir_corrected = nir - (hs_norm * hs_scalars[Inir])

    # -----Replace previously 0 values with 0 to signify no-data
    b_corrected[b==0] = 0
    g_corrected[g==0] = 0
    r_corrected[r==0] = 0
    nir_corrected[nir==0] = 0
        
    # -----Plot original and corrected images and band histograms
    if plot_results==True:
        fig1, ((ax1, ax2),(ax3,ax4)) = plt.subplots(2,2, figsize=(16,12), gridspec_kw={'height_ratios': [3, 1]})
        plt.rcParams.update({'font.size': 14, 'font.serif': 'Arial'})
        # original image
        ax1.imshow(np.dstack([r, g, b]),
                   extent=(np.min(im_x)/1000, np.max(im_x)/1000, np.min(im_y)/1000, np.max(im_y)/1000))
        ax1.plot([x/1000 for x in SCA.exterior.xy[0]], [y/1000 for y in SCA.exterior.xy[1]], color='black', linewidth=2, label='SCA')
        ax1.set_xlabel('Northing [km]')
        ax1.set_ylabel('Easting [km]')
        ax1.set_title('Original image')
        # corrected image
        ax2.imshow(np.dstack([r_corrected, g_corrected, b_corrected]),
                   extent=(np.min(im_x)/1000, np.max(im_x)/1000, np.min(im_y)/1000, np.max(im_y)/1000))
        ax2.plot([x/1000 for x in SCA.exterior.xy[0]], [y/1000 for y in SCA.exterior.xy[1]], color='black', linewidth=2, label='SCA')
        ax2.set_xlabel('Northing [km]')
        ax2.set_title('Corrected image')
        # band histograms
        ax3.hist(nir[nir>0].flatten(), bins=100, histtype='step', linewidth=1, color='purple', label='NIR')
        ax3.hist(b[b>0].flatten(), bins=100, histtype='step', linewidth=1, color='blue', label='Blue')
        ax3.hist(g[g>0].flatten(), bins=100, histtype='step', linewidth=1, color='green', label='Green')
        ax3.hist(r[r>0].flatten(), bins=100, histtype='step', linewidth=1, color='red', label='Red')
        ax3.set_xlabel('Surface reflectance')
        ax3.set_ylabel('Pixel counts')
        ax3.grid()
        ax3.legend()
        ax4.hist(nir_corrected[nir_corrected>0].flatten(), bins=100, histtype='step', linewidth=1, color='purple', label='NIR')
        ax4.hist(b_corrected[b_corrected>0].flatten(), bins=100, histtype='step', linewidth=1, color='blue', label='Blue')
        ax4.hist(g_corrected[g_corrected>0].flatten(), bins=100, histtype='step', linewidth=1, color='green', label='Green')
        ax4.hist(r_corrected[r_corrected>0].flatten(), bins=100, histtype='step', linewidth=1, color='red', label='Red')
        ax4.set_xlabel('Surface reflectance')
        ax4.grid()
        fig1.tight_layout()
        plt.show()
    
    # -----save hillshade-corrected image to file
    # create output directory (if it does not already exist in file)
    if os.path.exists(out_path)==False:
        os.mkdir(out_path)
        print('created output directory:',out_path)
    # file name
    im_corrected_name = im_name[0:-4]+'_hs-corrected.tif'
    # metadata
    out_meta = im.meta.copy()
    out_meta.update({'driver':'GTiff',
                     'width':b_corrected.shape[1],
                     'height':b_corrected.shape[0],
                     'count':4,
                     'dtype':'float64',
                     'crs':im.crs,
                     'transform':im.transform})
    # write to file
    with rio.open(out_path+im_corrected_name, mode='w',**out_meta) as dst:
        dst.write_band(1,b_corrected)
        dst.write_band(2,g_corrected)
        dst.write_band(3,r_corrected)
        dst.write_band(4,nir_corrected)
    print('corrected image saved to file: '+im_corrected_name)
                    
    return im_corrected_name

# --------------------------------------------------
def create_AOI_elev_polys(AOI, im_path, im_fns, DEM):
    '''
    Function to generate a polygon of the top 20th and bottom percentile elevations
    within the defined Area of Interest (AOI).
    
    Parameters
    ----------
    AOI: geopandas.geodataframe.GeoDataFrame
        Area of interest used for masking images. Must be in same coordinate reference system (CRS) as the image
    im_path: str
        path in directory to the input images
    im_fns: list of str
        image file names located in im_path.
    DEM: xarray.DataSet
        digital elevation model
    
    Returns
    ----------
    polygons: list
        list of shapely.geometry.Polygons representing the top and bottom 20th percentiles of elevations in the AOI.
        Median value in each polygon will be used to adjust images, depending on the difference.
    im: xarray.DataArray
        image
    '''

    # -----Read one image that contains AOI to create polygon
    os.chdir(im_path)
    for i in range(0,len(im_fns)):
        # define image filename
        im_fn = im_fns[i]
        # open image
        im = rio.open(im_fn)
        # mask the image using AOI geometry
        mask = rio.features.geometry_mask(AOI.geometry,
                                       im.read(1).shape,
                                       im.transform,
                                       all_touched=False,
                                       invert=False)
        # check if any image values exist within AOI
        if (0 in mask.flatten()):
            break

    # -----Open image as xarray.DataArray
    im_rxr = rxr.open_rasterio(im_fn)
    # set no data values to NaN
    im_rxr = im_rxr.where(im_rxr!=-9999)
    # account for image scalar
    if np.nanmean(im_rxr.data[2]) > 1e3:
        im_rxr = im_rxr / 10000

    # -----Mask the DEM outside the AOI exterior
    mask_AOI = rio.features.geometry_mask(AOI.geometry,
                                  out_shape=(len(DEM.y), len(DEM.x)),
                                  transform=DEM.transform,
                                  invert=True)
    # convert maskto xarray DataArray
    mask_AOI = xr.DataArray(mask_AOI , dims=("y", "x"))
    # mask DEM values outside the AOI
    DEM_AOI = DEM.where(mask_AOI == True)

    # -----Interpolate DEM to the image coordinates
    band, x, y = im_rxr.indexes.values() # grab indices of image
    DEM_AOI_interp = DEM_AOI.interp(x=x, y=y, method="nearest") # interpolate DEM to image coordinates

    # -----Top elevations polygon
    # mask the bottom percentile of elevations in the DEM
    DEM_bottom_P = np.nanpercentile(DEM_AOI_interp.elevation.data.flatten(), 80)
    mask = xr.where(DEM_AOI_interp > DEM_bottom_P, 1, 0).elevation.data[0]
    # convert mask to polygon
    # adapted from: https://rocreguant.com/convert-a-mask-into-a-polygon-for-images-using-shapely-and-rasterio/1786/
    polygons_top = []
    for s, value in rio.features.shapes(mask.astype(np.int16), mask=(mask >0), transform=im.transform):
        polygons_top.append(shape(s))
    polygons_top = MultiPolygon(polygons_top)
    
    # -----Bottom elevations polygon
    # mask the top 80th percentile of elevations in the DEM
    DEM_bottom_P = np.nanpercentile(DEM_AOI_interp.elevation.data.flatten(), 20)
    mask = xr.where(DEM_AOI_interp < DEM_bottom_P, 1, 0).elevation.data[0]
    # convert mask to polygon
    # adapted from: https://rocreguant.com/convert-a-mask-into-a-polygon-for-images-using-shapely-and-rasterio/1786/
    polygons_bottom = []
    for s, value in rio.features.shapes(mask.astype(np.int16), mask=(mask >0), transform=im.transform):
        polygons_bottom.append(shape(s))
    polygons_bottom = MultiPolygon(polygons_bottom)
        
    return polygons_top, polygons_bottom, im_fn, im_rxr
    
    
# --------------------------------------------------
def adjust_image_radiometry(im_fn, im_path, polygon_top, polygon_bottom, out_path, skip_clipped, plot_results):
    '''
    Adjust PlanetScope image band radiometry using the band values in a defined snow-covered area (SCA) and the expected surface reflectance of snow.
    
    Parameters
    ----------
    im_fn: str
        file name of the input image
    im_path: str
        path in directory to the input image
    polygon_top: shapely.geometry.polygon.Polygon
        polygon of the top 20th percentile of elevations in the AOI
    polygon_bottom: shapely.geometry.polygon.Polygon
        polygon of the bottom 20th percentile of elevations in the AOI
    out_path: str
        path in directory where adjusted image file will be saved
    skip_clipped: bool
        whether to skip images where bands appear "clipped"
    plot_results: bool
        whether to plot results to a matplotlib.pyplot.figure
    
    Returns
    ----------
    im_adj_name: str
        file name of the adjusted image saved to file
    im_adj_method: str
        method used to adjust image ('SNOW' = using the predicted surface reflectance of snow, 'ICE' = using the predicted surface reflectance of ice)
    '''
    
    # -----Create output directory if it does not exist
    if os.path.isdir(out_path)==0:
        os.mkdir(out_path)
        print('Created directory for adjusted images: ' + out_path)
    
    # -----Check if adjusted image file exist
    im_adj_fn = im_fn[0:-4]+'_adj.tif' # adjusted image file name
    if os.path.exists(out_path + im_adj_fn)==True:
    
        print('adjusted image already exists... loading from file.')
        
        # load adjusted image from file
        im_adj = rxr.open_rasterio(out_path + im_adj_fn)
        # replace no data values with NaN
        im_adj = im_adj.where(im_adj!=-9999)
        # account for image scalar multiplier if necessary
        if np.nanmean(im_adj.data[2]) > 1e3:
            im_adj = im_adj / 10000
            
        im_adj_method = 'N/A'
            
        return im_adj_fn, im_adj_method

    else:
            
        # -----Load input image
        im_rxr = rxr.open_rasterio(im_path + im_fn)
        im_rio = rio.open(im_path + im_fn)
        # set no data values to NaN
        im_rxr = im_rxr.where(im_rxr!=-9999)
        # account for image scalar multiplier if necessary
        im_scalar = 10000
        if np.nanmean(im_rxr.data[2]) > 1e3:
            im_rxr = im_rxr / im_scalar
        # define bands
        b = im_rxr.data[0]
        g = im_rxr.data[1]
        r = im_rxr.data[2]
        nir = im_rxr.data[3]
            
        # -----Return if image bands are likely clipped
        if skip_clipped==True:
            if ((np.nanmax(b) < 0.8) or (np.nanmax(g) < 0.8) or (np.nanmax(r) < 0.8)):
                print('image bands appear clipped... skipping.')
                im_adj_fn = 'N/A'
                return im_adj_fn

        # -----Return if image does not contain polygon
        # mask the image using polygon geometries
        mask_top = rio.features.geometry_mask([polygon_top],
                                       np.shape(b),
                                       im_rio.transform,
                                       all_touched=False,
                                       invert=False)
        mask_bottom = rio.features.geometry_mask([polygon_bottom],
                                       np.shape(b),
                                       im_rio.transform,
                                       all_touched=False,
                                       invert=False)
        # skip if image does not contain polygon
        if (0 not in mask_top.flatten()) or (0 not in mask_bottom.flatten()):
            print('image does not contain polygons... skipping.')
            im_adj_fn, im_adj_method = 'N/A', 'N/A'
            return im_adj_fn, im_adj_method
            
        # -----Return if no real values exist within the SCA
        if (np.nanmean(b)==0) or (np.isnan(np.nanmean(b))):
            print('image does not contain any real values within the polygon... skipping.')
            im_adj_fn, im_adj_method = 'N/A', 'N/A'
            return im_adj_fn, im_adj_method
            
        # -----Filter image points outside the top polygon
        b_top_polygon = b[mask_top==0]
        g_top_polygon = g[mask_top==0]
        r_top_polygon = r[mask_top==0]
        nir_top_polygon = nir[mask_top==0]
        
        # -----Filter image points outside the bottom polygon
        b_bottom_polygon = b[mask_bottom==0]
        g_bottom_polygon = g[mask_bottom==0]
        r_bottom_polygon = r[mask_bottom==0]
        nir_bottom_polygon = nir[mask_bottom==0]
        
        # -----Calculate median value for each polygon and the mean difference between the two
        SR_top_median = np.mean([np.nanmedian(b_top_polygon), np.nanmedian(g_top_polygon),
                                   np.nanmedian(r_top_polygon), np.nanmedian(nir_top_polygon)])
        SR_bottom_median = np.mean([np.nanmedian(b_bottom_polygon), np.nanmedian(g_bottom_polygon),
                                   np.nanmedian(r_bottom_polygon), np.nanmedian(nir_bottom_polygon)])
        difference = np.mean([np.nanmedian(b_top_polygon) - np.nanmedian(b_bottom_polygon),
                                np.nanmedian(g_top_polygon) - np.nanmedian(g_bottom_polygon),
                                np.nanmedian(r_top_polygon) - np.nanmedian(r_bottom_polygon),
                                np.nanmedian(nir_top_polygon) - np.nanmedian(nir_bottom_polygon)])
        if (SR_top_median < 0.45) and (difference < 0.1):
            im_adj_method = 'ICE'
        else:
            im_adj_method = 'SNOW'
    
        # -----Define the desired bright and dark surface reflectance values
        #       at the top elevations based on the method determined above
        if im_adj_method=='SNOW':
            
            # define desired SR values at the bright area and darkest point for each band
            # bright area
            bright_b_adj = 0.94
            bright_g_adj = 0.95
            bright_r_adj = 0.94
            bright_nir_adj = 0.78
            # dark point
            dark_adj = 0.0
        
        
        elif im_adj_method=='ICE':
                    
            # define desired SR values at the bright area and darkest point for each band
            # bright area
            bright_b_adj = 0.58
            bright_g_adj = 0.59
            bright_r_adj = 0.57
            bright_nir_adj = 0.40
            # dark point
            dark_adj = 0.0
        
        # -----Adjust surface reflectance values
        # band_adjusted = band*A - B
        # A = (bright_adjusted - dark_adjusted) / (bright - dark)
        # B = (dark*bright_adjusted - bright*dark_adjusted) / (bright - dark)
        # blue band
        bright_b = np.nanmedian(b_top_polygon) # SR at bright point
        dark_b = np.nanmin(b) # SR at darkest point
        A = (bright_b_adj - dark_adj) / (bright_b - dark_b)
        B = (dark_b*bright_b_adj - bright_b*dark_adj) / (bright_b - dark_b)
        b_adj = (b * A) - B
        b_adj = np.where(b==0, np.nan, b_adj) # replace no data values with nan
        # green band
        bright_g = np.nanmedian(g_top_polygon) # SR at bright point
        dark_g = np.nanmin(g) # SR at darkest point
        A = (bright_g_adj - dark_adj) / (bright_g - dark_g)
        B = (dark_g*bright_g_adj - bright_g*dark_adj) / (bright_g - dark_g)
        g_adj = (g * A) - B
        g_adj = np.where(g==0, np.nan, g_adj) # replace no data values with nan
        # red band
        bright_r = np.nanmedian(r_top_polygon) # SR at bright point
        dark_r = np.nanmin(r) # SR at darkest point
        A = (bright_r_adj - dark_adj) / (bright_r - dark_r)
        B = (dark_r*bright_r_adj - bright_r*dark_adj) / (bright_r - dark_r)
        r_adj = (r * A) - B
        r_adj = np.where(r==0, np.nan, r_adj) # replace no data values with nan
        # nir band
        bright_nir = np.nanmedian(nir_top_polygon) # SR at bright point
        dark_nir = np.nanmin(nir) # SR at darkest point
        A = (bright_nir_adj - dark_adj) / (bright_nir - dark_nir)
        B = (dark_nir*bright_nir_adj - bright_nir*dark_adj) / (bright_nir - dark_nir)
        nir_adj = (nir * A) - B
        nir_adj = np.where(nir==0, np.nan, nir_adj) # replace no data values with nan
        
        # -----Save adjusted raster image to file
        # reformat bands for saving as int data type
        b_save = b_adj * im_scalar
        b_save[np.isnan(b)] = -9999
        g_save = g_adj * im_scalar
        g_save[np.isnan(g)] = -9999
        r_save = r_adj * im_scalar
        r_save[np.isnan(r)] = -9999
        nir_save = nir_adj * im_scalar
        nir_save[np.isnan(nir)] = -9999
        # copy metadata
        out_meta = im_rio.meta.copy()
        out_meta.update({'driver': 'GTiff',
                         'width': b_save.shape[1],
                         'height': b_save.shape[0],
                         'count': 4,
                         'dtype': 'uint16',
                         'crs': im_rio.crs,
                         'transform': im_rio.transform})
        # write to file
        with rio.open(out_path+im_adj_fn, mode='w',**out_meta) as dst:
            # write bands - multiply bands by im_scalar and convert datatype to uint64 to decrease file size
            dst.write_band(1, b_save)
            dst.write_band(2, g_save)
            dst.write_band(3, r_save)
            dst.write_band(4, nir_save)
        print('adjusted image saved to file: ' + im_adj_fn)

    # -----Plot RGB images and band histograms for the original and adjusted image
    if plot_results:
        fig, ((ax1, ax2),(ax3,ax4)) = plt.subplots(2,2, figsize=(16,12), gridspec_kw={'height_ratios': [3, 1]})
        plt.rcParams.update({'font.size': 12, 'font.serif': 'Arial'})
        # original image
        im_original = ax1.imshow(np.dstack([im_rxr.data[2], im_rxr.data[1], im_rxr.data[0]]),
                    extent=(np.min(im_rxr.x.data)/1e3, np.max(im_rxr.x.data)/1e3, np.min(im_rxr.y.data)/1e3, np.max(im_rxr.y.data)/1e3))
        count=0
#        for geom in polygon_top.geoms:
#            xs, ys = geom.exterior.xy
#            if count==0:
#                ax1.plot([x/1000 for x in xs], [y/1000 for y in ys], color='c', label='top polygon(s)')
#            else:
#                ax1.plot([x/1000 for x in xs], [y/1000 for y in ys], color='c', label='_nolegend_')
#            count+=1
#        for geom in polygon_bottom.geoms:
#            xs, ys = geom.exterior.xy
#            if count==0:
#                ax1.plot([x/1000 for x in xs], [y/1000 for y in ys], color='orange', label='bottom polygon(s)')
#            else:
#                ax1.plot([x/1000 for x in xs], [y/1000 for y in ys], color='orange', label='_nolegend_')
#            count+=1
        ax1.legend()
        ax1.set_xlabel('Easting [km]')
        ax1.set_ylabel('Northing [km]')
        ax1.set_title('Raw image')
        # adjusted image
        ax2.imshow(np.dstack([r_adj, g_adj, b_adj]),
            extent=(np.min(im_rxr.x.data)/1e3, np.max(im_rxr.x.data)/1e3,
                    np.min(im_rxr.y.data)/1e3, np.max(im_rxr.y.data)/1e3))
        count=0
        ax2.set_xlabel('Easting [km]')
        ax2.set_title('Adjusted image')
        # band histograms
        ax3.hist(nir[nir>0].flatten(), bins=100, histtype='step', linewidth=1, color='purple', label='NIR')
        ax3.hist(b[b>0].flatten(), bins=100, histtype='step', linewidth=1, color='blue', label='Blue')
        ax3.hist(g[g>0].flatten(), bins=100, histtype='step', linewidth=1, color='green', label='Green')
        ax3.hist(r[r>0].flatten(), bins=100, histtype='step', linewidth=1, color='red', label='Red')
        ax3.set_xlabel('Surface reflectance')
        ax3.set_ylabel('Pixel counts')
        ax3.grid()
        ax3.legend()
        ax4.hist(nir_adj[nir_adj>0].flatten(), bins=100, histtype='step', linewidth=1, color='purple', label='NIR')
        ax4.hist(b_adj[b_adj>0].flatten(), bins=100, histtype='step', linewidth=1, color='blue', label='Blue')
        ax4.hist(g_adj[g_adj>0].flatten(), bins=100, histtype='step', linewidth=1, color='green', label='Green')
        ax4.hist(r_adj[r_adj>0].flatten(), bins=100, histtype='step', linewidth=1, color='red', label='Red')
        ax4.set_xlabel('Surface reflectance')
        ax4.grid()
        fig.tight_layout()
        plt.show()
            
    return im_adj_fn, im_adj_method

# --------------------------------------------------
def query_GEE_for_DEM(AOI):
    '''Query GEE for the ASTER Global DEM, clip to the AOI, and return as a numpy array.
    
    Parameters
    ----------
    AOI: geopandas.geodataframe.GeoDataFrame
        area of interest used for clipping the DEM
    
    Returns
    ----------
    DEM_ds: xarray.Dataset
        elevations extracted within the AOI
    AOI_UTM: geopandas.geodataframe.GeoDataFrame
        AOI reprojected to the appropriate UTM coordinate reference system
    '''
    
    # -----Reformat AOI for clipping DEM
    # reproject AOI to WGS 84 for compatibility with DEM
    AOI_WGS = AOI.to_crs(4326)
    # reformat AOI_WGS bounding box as ee.Geometry for clipping DEM
    AOI_WGS_bb_ee = ee.Geometry.Polygon(
                            [[[AOI_WGS.geometry.bounds.minx[0], AOI_WGS.geometry.bounds.miny[0]],
                              [AOI_WGS.geometry.bounds.maxx[0], AOI_WGS.geometry.bounds.miny[0]],
                              [AOI_WGS.geometry.bounds.maxx[0], AOI_WGS.geometry.bounds.maxy[0]],
                              [AOI_WGS.geometry.bounds.minx[0], AOI_WGS.geometry.bounds.maxy[0]],
                              [AOI_WGS.geometry.bounds.minx[0], AOI_WGS.geometry.bounds.miny[0]]]
                            ]).buffer(1000)

    # -----Query GEE for DEM, clip to AOI
    DEM = ee.Image("NASA/ASTER_GED/AG100_003").clip(AOI_WGS_bb_ee).select('elevation')
    
    # -----Grab UTM projection from images, reproject DEM and AOI
    AOI_WGS_centroid = [AOI_WGS.geometry[0].centroid.xy[0][0],
                        AOI_WGS.geometry[0].centroid.xy[1][0]]
    epsg_UTM = convert_wgs_to_utm(AOI_WGS_centroid[0], AOI_WGS_centroid[1])
    AOI_UTM = AOI.to_crs(str(epsg_UTM))
    
    # -----Convert DEM to xarray.Dataset
    DEM = DEM.set('system:time_start', 0) # set an arbitrary time
    DEM_ds = DEM.wx.to_xarray(scale=30, crs='EPSG:'+str(epsg_UTM))
    
    return DEM_ds, AOI_UTM
    
# --------------------------------------------------
def crop_images_to_AOI(im_path, im_fns, AOI):
    '''
    Crop images to AOI.
    
    Parameters
    ----------
    im_path: str
        path in directory to input images
    im_fns: str array
        file names of images to crop
    AOI: geopandas.geodataframe.GeoDataFrame
        cropping region - everything outside the AOI will be masked. Only the exterior bounds used for cropping (no holes). AOI must be in the same CRS as the images.
    
    Returns
    ----------
    cropped_im_path: str
        path in directory to cropped images
    '''
    
    # make folder for cropped images if it does not exist
    cropped_im_path = im_path + "../cropped/"
    if os.path.isdir(cropped_im_path)==0:
        os.mkdir(cropped_im_path)
        print(cropped_im_path+" directory made")
    
    # loop through images
    for im_fn in im_fns:

        # open image
        im = rio.open(im_path + im_fn)

        # check if file exists in directory already
        cropped_im_fn = cropped_im_path + im_fn[0:15] + "_crop.tif"
        if os.path.exists(cropped_im_fn)==True:
            print("cropped image already exists in directory...skipping.")
        else:
            # mask image pixels outside the AOI exterior
#            AOI_bb = [AOI.bounds]
            out_image, out_transform = mask(im, AOI.buffer(100), crop=True)
            out_meta = im.meta.copy()
            out_meta.update({"driver": "GTiff",
                         "height": out_image.shape[1],
                         "width": out_image.shape[2],
                         "transform": out_transform})
            with rio.open(cropped_im_fn, "w", **out_meta) as dest:
                dest.write(out_image)
            print(cropped_im_fn + " saved")
            
    return cropped_im_path

# --------------------------------------------------
#def plot_im_classified_histograms(im, im_dt, im_classified, snow_elev, b, g, r, nir, DEM, DEM_x, DEM_y):
#    '''
#    Plot classified images and histograms of snow elevation distribution
#
#    Parameters
#    ----------
#    im: rasterio object
#        input image
#    im_x: numpy.array
#        x coordinates of input image
#    im_y: numpy.array
#        y coordinates of image
#    im_dt: numpy.datetime64
#        datetime of the image capture
#    im_classified:
#
#    snow_elev:
#
#    b:
#
#    g:
#
#    r:
#
#    nir:
#
#    DEM:
#
#    DEM_x:
#
#    DEM_y:
#
#
#    Returns
#    ----------
#    fig: matplotlib.figure
#        resulting figure handle
#
#    '''
#
#    # -----Grab 2nd percentile snow elevation
#    P = np.percentile(snow_elev, 2)
#
#    # -----Plot
#    fig, ((ax1, ax2), (ax3, ax4)) = plt.subplots(2, 2, figsize=(10,10), gridspec_kw={'height_ratios': [3, 1]})
#    plt.rcParams.update({'font.size': 14, 'font.sans-serif': 'Arial'})
#    # define x and y limits
#    xmin, xmax = np.min(im_x)/1000, np.max(im_x)/1000
#    ymin, ymax = np.min(im_y)/1000, np.max(im_y)/1000
#    # RGB image
#    ax1.imshow(np.dstack([r, g, b]), extent=(xmin, xmax, ymin, ymax))
#    ax1.set_xlabel("Easting [km]")
#    ax1.set_ylabel("Northing [km]")
#    # define colors for plotting
#    color_snow = '#4eb3d3'
#    color_ice = '#084081'
#    color_rock = '#fdbb84'
#    color_water = '#bdbdbd'
#    # snow
#    if any(im_classified.flatten()==1):
#        ax2.imshow(np.where(im_classified == 1, 1, np.nan), cmap=matplotlib.colors.ListedColormap([color_snow, 'white']),
#                    extent=(xmin, xmax, ymin, ymax))
#        ax2.scatter(0, 0, color=color_snow, s=50, label='snow') # plot dummy point for legend
#    if any(im_classified.flatten()==2):
#        ax2.imshow(np.where(im_classified == 2, 4, np.nan), cmap=matplotlib.colors.ListedColormap([color_snow, 'white']),
#                    extent=(xmin, xmax, ymin, ymax))
#    # ice
#    if any(im_classified.flatten()==3):
#        ax2.imshow(np.where(im_classified == 3, 1, np.nan), cmap=matplotlib.colors.ListedColormap([color_ice, 'white']),
#                    extent=(xmin, xmax, ymin, ymax))
#        ax2.scatter(0, 0, color=color_ice, s=50, label='ice') # plot dummy point for legend
#    # rock/debris
#    if any(im_classified.flatten()==4):
#        ax2.imshow(np.where(im_classified == 4, 1, np.nan), cmap=matplotlib.colors.ListedColormap([color_rock, 'white']),
#                    extent=(xmin, xmax, ymin, ymax))
#        ax2.scatter(0, 0, color=color_rock, s=50, label='rock') # plot dummy point for legend
#    # water
#    if any(im_classified.flatten()==5):
#        ax2.imshow(np.where(im_classified == 5, 10, np.nan), cmap=matplotlib.colors.ListedColormap([color_water, 'white']),
#                    extent=(xmin, xmax, ymin, ymax))
#        ax2.scatter(0, 0, color=color_water, s=50, label='water') # plot dummy point for legend
#    # snow elevation contour
#    cs = ax2.contour(DEM_x/1000, DEM_y/1000, np.flipud(DEM.squeeze()), [P], colors=['black'])
#    ax2.legend(loc='lower left')
#    ax2.set_xlabel("Easting [km]")
#    ax2.set_xlim(xmin, xmax)
#    ax2.set_ylim(ymin, ymax)
#    # image bands histogram
#    h_b = ax3.hist(b[b!=0].flatten(), color='blue', histtype='step', linewidth=2, bins=100, label="blue")
#    h_g = ax3.hist(g[g!=0].flatten(), color='green', histtype='step', linewidth=2, bins=100, label="green")
#    h_r = ax3.hist(r[r!=0].flatten(), color='red', histtype='step', linewidth=2, bins=100, label="red")
#    h_nir = ax3.hist(nir[nir!=0].flatten(), color='brown', histtype='step', linewidth=2, bins=100, label="NIR")
#    ax3.set_xlabel("Surface reflectance")
#    ax3.set_ylabel("Pixel counts")
#    ax3.legend(loc='upper left')
#    ax3.set_ylim(0,np.max([h_nir[0][1:], h_g[0][1:], h_r[0][1:], h_b[0][1:]])+5000)
#    ax3.grid()
#    # snow elevations histogram
#    ax4.hist(snow_elev.flatten(), bins=100, color=color_snow)
#    ax4.set_xlabel("Elevation [m]")
#    ax4.grid()
#    ymin, ymax = ax4.get_ylim()[0], ax4.get_ylim()[1]
#    ax4.plot((P, P), (ymin, ymax), color='black', label='P$_{2}$')
#    ax4.set_ylim(ymin, ymax)
#    ax4.legend(loc='lower right')
#    fig.tight_layout()
#    fig.suptitle(im_dt)
#    plt.show()
#
#    # extract contour vertices
#    p = cs.collections[0].get_paths()[0]
#    v = p.vertices
#    x = v[:,0]
#    y = v[:,1]
#
#    return fig

# --------------------------------------------------
def classify_image(im_fn, im_path, clf, feature_cols, crop_to_AOI, AOI, out_path):
    '''
    Function to classify input image using a pre-trained classifier
    
    Parameters
    ----------
    im_fn: str
        file name of input image
    im_path: str
        path to image file in directory
    clf: sklearn.classifier
        previously trained SciKit Learn Classifier
    feature_cols: array of pandas.DataFrame columns, e.g. ['blue', 'green', 'red']
        features used by classifier
    out_path: str
        path to save classified images
    crop_to_AOI: bool
        whether to mask everywhere outside the AOI before classifying
    AOI: geopandas.geodataframe.GeoDataFrame
        cropping region - everything outside the AOI will be masked if crop_to_AOI==True. AOI must be in the same CRS as the images.
    
    plot_output: bool
        whether to plot RGB and classified image
        
    Returns
    ----------
    im_x: numpy.array
        x coordinates of input image
    im_y: numpy.array
        y coordinates of image
    snow: numpy.array
        binary array of predicted snow presence in input image, where 0 = no snow and 1 = snow
    '''

    # -----Make directory for snow images (if it does not already exist in file)
    if os.path.exists(out_path)==False:
        os.mkdir(out_path)
        print("Made directory for classified snow images:" + out_path)
        
    # -----Open input image
    im = rxr.open_rasterio(im_path + im_fn) # open image as xarray.DataArray
    im_rio = rio.open(im_path + im_fn) # open image as rasterio read object
    im = im.where(im!=-9999) # replace no data values with NaN
    # account for image scalar multiplier if necessary
    im_scalar = 10000
    if np.nanmean(im.data[0])>1e3:
        im = im / im_scalar
        
    # -----Check if classified snow image exists in directory already
    im_classified_fn = im_fn[0:-4] + "_classified.tif"
    if os.path.exists(out_path + im_classified_fn):
    
        print("Classified snow image already exists in directory, skipping...")
        
    else:
        
        # -----Determine image bands
        b = im.data[0]
        g = im.data[1]
        r = im.data[2]
        nir = im.data[3]
    
        # -----Calculate NDSI using red and NIR bands
        ndsi = (r - nir) / (r + nir)
        
        # -----Mask the image using the AOI geometry
        if crop_to_AOI:
            # create pandas.GeoDataFrame with geometry = AOI exterior
            d = {'geometry': [Polygon(AOI.exterior[0])]}
            gdf = gpd.GeoDataFrame(d, crs="EPSG:"+str(AOI.crs.to_epsg()))
            mask = rio.features.geometry_mask(gdf.geometry,
                b.shape,
                im_rio.transform,
                all_touched=False,
                invert=False)
            b = np.where(mask==0, b, np.nan)
            g = np.where(mask==0, g, np.nan)
            r = np.where(mask==0, r, np.nan)
            nir = np.where(mask==0, nir, np.nan)
            ndsi = np.where(mask==0, ndsi, np.nan)
        
        # Find indices of real numbers (no NaNs allowed in classification)
        I_real = np.where((~np.isnan(b)) & (~np.isnan(g)) & (~np.isnan(r)) & (~np.isnan(nir)) & (~np.isnan(ndsi)))
        
        # save in Pandas dataframe
        df = pd.DataFrame()
        df['blue'] = b[I_real].flatten()
        df['green'] = g[I_real].flatten()
        df['red'] = r[I_real].flatten()
        df['NIR'] = nir[I_real].flatten()
        df['NDSI'] = ndsi[I_real].flatten()
        df['moy'] = float(im_fn[4:6])

        # classify image
        try:
            array_classified = clf.predict(df[feature_cols])
        except:
            print("Error in classification... skipping image.")
            return None, None
        
        # reshape from flat array to original shape
        im_classified = np.zeros((np.shape(b)[0], np.shape(b)[1]))
        im_classified[:] = np.nan
        im_classified[I_real] = array_classified
        
        # replace nan values with -9999 in order to save file with datatype int16
        im_classified[np.isnan(im_classified)] = -9999
        
        # save to file
        with rio.open(out_path + im_classified_fn,'w',
                      driver='GTiff',
                      height=np.shape(im.data[0])[0],
                      width=np.shape(im.data[0])[1],
                      dtype='int16',
                      count=1,
                      crs=im_rio.crs,
                      transform=im_rio.transform) as dst:
            dst.write(im_classified, 1)
        print("Classified image saved to file:",im_classified_fn)
                
    return im_classified_fn, im

# --------------------------------------------------
def delineate_snow_line(im_fn, im_path, im_classified_fn, im_classified_path, AOI, DEM):
    '''
    Parameters
    ----------
    im_fn:
    
    im_path:
    
    im_classified_fn:
    
    im_classified_path:
    
    AOI:
    
    DEM
    
    Returns
    ----------
    fig:
    
    ax:
    
    sl_est:
    
    sl_est_elev:
    
    '''

    # -----Open images
    # VNIR image
    im = rxr.open_rasterio(im_path + im_fn) # open image as xarray.DataArray
    im = im.where(im!=-9999) # remove no data values
    if np.nanmean(im) > 1e3:
        im = im / 1e4 # account for surface reflectance scalar multiplier
    date = im_fn[0:8] # grab image capture date from file name

    # classified image
    im_classified = rxr.open_rasterio(im_classified_path + im_classified_fn) # open image as xarray.DataArray
    # create no data mask
    no_data_mask = xr.where(im_classified==-9999, 1, 0).data[0]
    # convert to polygons
    no_data_polygons = []
    for s, value in rio.features.shapes(no_data_mask.astype(np.int16),
                                        mask=(no_data_mask >0),
                                        transform=rio.open(im_path + im_fn).transform):
        no_data_polygons.append(shape(s))
    no_data_polygons = MultiPolygon(no_data_polygons)
    # mask no data points in classified image
    im_classified = im_classified.where(im_classified!=-9999) # now, remove no data values
        
    # -----Mask the DEM using the AOI
    # create AOI mask
    mask_AOI = rio.features.geometry_mask(AOI.geometry,
                                      out_shape=(len(DEM.y), len(DEM.x)),
                                      transform=DEM.transform,
                                      invert=True)
    # convert mask to xarray DataArray
    mask_AOI = xr.DataArray(mask_AOI , dims=("y", "x"))
    # mask DEM values outside the AOI
    DEM_AOI = DEM.where(mask_AOI == True)

    # -----Interpolate DEM to the image coordinates
    im_classified = im_classified.squeeze(drop=True) # remove unecessary dimensions
    x, y = im_classified.indexes.values() # grab indices of image
    DEM_AOI_interp = DEM_AOI.interp(x=x, y=y, method="nearest") # interpolate DEM to image coordinates

    # -----Determine snow covered elevations
    # mask pixels not classified as snow
    DEM_AOI_interp_snow = DEM_AOI_interp.where(im_classified<=2)
    # create array of snow-covered pixel elevations
    snow_est_elev = DEM_AOI_interp_snow.elevation.data.flatten()

    # -----Create elevation histograms
    # determine bins to use in histograms
    elev_min = np.fix(np.nanmin(DEM_AOI_interp.elevation.data.flatten())/10)*10
    elev_max = np.round(np.nanmax(DEM_AOI_interp.elevation.data.flatten())/10)*10
    bin_edges = np.linspace(elev_min, elev_max, num=int((elev_max-elev_min)/10 + 1))
    bin_centers = (bin_edges[1:] + bin_edges[0:-1]) / 2
    # calculate elevation histograms
    H_DEM = np.histogram(DEM_AOI_interp.elevation.data.flatten(), bins=bin_edges)[0]
    H_snow_est_elev = np.histogram(snow_est_elev, bins=bin_edges)[0]
    H_snow_est_elev_norm = H_snow_est_elev / H_DEM
    
    # -----Make all pixels at elevations >75% snow coverage snow
    # determine elevation with > 75% snow coverage
    elev_75_snow = bin_centers[np.where(H_snow_est_elev_norm > 0.75)[0][0]]
    # set all pixels above the elev_75_snow to snow (1)
    im_classified_adj = xr.where(DEM_AOI_interp.elevation > elev_75_snow, 1, im_classified) # set all values above elev_75_snow to snow (1)
    im_classified_adj = im_classified_adj.squeeze(drop=True) # drop unecessary dimensions
    
    # -----Determine snow line
    # generate and filter binary snow matrix
    # create binary snow matrix
    im_binary = xr.where(im_classified_adj  > 2, 1, 0).data
    # apply median filter to binary image with kernel_size of 33 pixels (~99 m)
    im_binary_filt = medfilt(im_binary, kernel_size=33)
    # fill holes in binary image (0s within 1s = 1)
    im_binary_filt_no_holes = binary_fill_holes(im_binary_filt)
    # find contours at a constant value of 0.5 (between 0 and 1)
    contours = find_contours(im_binary_filt_no_holes, 0.5)
    # convert contour points to image coordinates
    contours_coords = []
    for contour in contours:
        ix = np.round(contour[:,1]).astype(int)
        iy = np.round(contour[:,0]).astype(int)
        coords = (im.isel(x=ix, y=iy).x.data, # image x coordinates
                  im.isel(x=ix, y=iy).y.data) # image y coordinates
        # zip points together
        xy = list(zip([x for x in coords[0]],
                      [y for y in coords[1]]))
        contours_coords = contours_coords + [xy]
    # create snow-covered polygons
    c_polys = []
    for c in contours_coords:
        c_points = [Point(x,y) for x,y in c]
        c_poly = Polygon([[p.x, p.y] for p in c_points])
        c_polys = c_polys + [c_poly]
    # only save the largest polygon
    if len(c_polys) > 1:
        # calculate polygon areas
        areas = np.array([poly.area for poly in c_polys])
        # grab top 3 areas with their polygon indices
        areas_max = sorted(zip(areas, np.arange(0,len(c_polys))), reverse=True)[:1]
        # grab indices
        ic_polys = [x[1] for x in areas_max]
        # grab polygons at indices
        c_polys = [c_polys[i] for i in ic_polys]
    # extract coordinates in polygon
    polys_coords = [list(zip(c.exterior.coords.xy[0], c.exterior.coords.xy[1]))  for c in c_polys]

    # extract snow lines (sl) from contours
    # filter contours using no data and AOI masks (i.e., along glacier outline or data gaps)
    sl_est = [] # initialize list of snow lines
    min_sl_length = 100 # minimum snow line length
    for c in polys_coords:
        # create array of points
        c_points =  [Point(x,y) for x,y in c]
        # loop through points
        line_points = [] # initialize list of points to use in snow line
        for point in c_points:
            # calculate distance from the point to the no data polygons and the AOI boundary
            distance_no_data = no_data_polygons.distance(point)
            distance_AOI = AOI.boundary[0].distance(point)
            # only include points 100 m from both
            if (distance_no_data >= 100) and (distance_AOI >=100):
                line_points = line_points + [point]
        if line_points: # if list of line points is not empty
            if len(line_points) > 1: # must have at least two points to create a LineString
                line = LineString([(p.xy[0][0], p.xy[1][0]) for p in line_points])
                if line.length > min_sl_length:
                    sl_est = sl_est + [line]
                    
    # split lines with points more than 100 m apart and filter by length
#    sl_est_split = [] # initialize list of filtered snow lines
#    min_sl_length = 200
#    for line in sl_est:
#        # extract line x and y coordinates
#        coords = list(line.coords)
#        # initialize binary array of where to split
#        split_list = np.zeros(len(line.coords))
#        # loop through points
#        for i in np.arange(1,len(coords)):
#            if i!=0:
#                point = Point(coords[i])
#                # calculate distance between point and previous point
#                distance = point.distance(Point(coords[i-1]))
#                # set split to 1 if distance is greater than 100 m
#                if distance > 100:
#                    split_list[i] = 1
#        if np.any(split_list==1):
#            # initialize binary list of where to split the line
#            isplit = np.ravel(np.where(split_list==1))
#            for i in np.arange(0,len(isplit)):
#                if i==0:
#                    if len(coords[:isplit[i]+1]) > 1: # must have at least 2 points in LineString
#                        line_split = LineString(coords[:isplit[i]+1])
#                    else:
#                        line_split = None
#                else:
#                    if len(coords[isplit[i-1]+1:isplit[i]]) > 1: # must have at least 2 points in LineString
#                        line_split = LineString(coords[isplit[i-1]+1:isplit[i]+1])
#                    else:
#                        line_split = None
#                # concatenate split line to sl_est_filt if greater than min_sl_length
#                if line_split is not None:
#                    if line_split.length > min_sl_length:
#                        sl_est_split = sl_est_split + [line_split]
#        else:
#            # concatenate line to sl_est_filt if greater than min_sl_length
#            if line.length > min_sl_length:
#                sl_est_split = sl_est_split + [line]
                        
    # -----Interpolate elevations at snow line coordinates
    # compile all line coordinates into arrays of x- and y-coordinates
    xpts, ypts = [], []
    for line in sl_est:
        xpts = xpts + [x for x in line.coords.xy[0]]
        ypts = ypts + [y for y in line.coords.xy[1]]
    xpts, ypts = np.array(xpts).flatten(), np.array(ypts).flatten()
    # interpolate elevation at snow line points
    sl_est_elev = [DEM.sel(x=x, y=y, method='nearest').elevation.data[0]
                   for x, y in list(zip(xpts, ypts))]
    
    # -----Plot results
    contour = None
    fig, ax, sl_points_AOI = plot_im_classified_histogram_contour(im, im_classified_adj, DEM, AOI, contour)
    # plot estimated snow line coordinates
    for line in sl_est:
        ax[0].plot([x/1e3 for x in xpts],
                   [y/1e3 for y in ypts],
                   '.', color='#f768a1', label='sl$_{estimated}$', markersize=1)
        ax[1].plot([x/1e3 for x in xpts],
                   [y/1e3 for y in ypts],
                   '.', color='#f768a1', label='_nolegend_', markersize=1)
    # add legends
    ax[0].legend(loc='best')
    ax[1].legend(loc='best')
    if contour is not None:
        ax[3].set_title('Contour = ' + str(np.round(contour,1)) + ' m')
    fig.suptitle(date)

    return fig, ax, sl_est, sl_est_elev

# --------------------------------------------------
def calculate_SCA(im, im_classified):
    '''Function to calculated total snow-covered area (SCA) from using an input image and a snow binary mask of the same resolution and grid.
    Parameters
    ----------
        im: rasterio object
            input image
        im_classified: numpy array
            classified image array with the same shape as the input image bands. Classes: snow = 1, shadowed snow = 2, ice = 3, rock/debris = 4.
    Returns
    ----------
        SCA: float
            snow-covered area in classified image [m^2]'''

    pA = im.res[0]*im.res[1] # pixel area [m^2]
    snow_count = np.count_nonzero(im_classified <= 2) # number of snow and shadowed snow pixels
    SCA = pA * snow_count # area of snow [m^2]

    return SCA

# --------------------------------------------------
def determine_snow_elevs(DEM, im, im_classified_fn, im_classified_path, im_dt, AOI, plot_output):
    '''Determine elevations of snow-covered pixels in the classified image.
    Parameters
    ----------
    DEM: xarray.Dataset
        digital elevation model
    im: xarray.DataArray
        input image used to classify snow
    im_classified_fn: str
        classified image file name
    im_classified_path: str
        path in directory to classified image
    im_dt: numpy.datetime64
        datetime of the image capture
    AOI: geopandas.GeoDataFrame
        area of interest
    plot_output: bool
        whether to plot the output RGB and snow classified image with histograms for surface reflectances of each band and the elevations of snow-covered pixels
        
    Returns
    ----------
    snow_elev: numpy array
        elevations at each snow-covered pixel
    '''
    
    # -----Set up original image
    # account for image scalar multiplier if necessary
    im_scalar = 10000
    if np.nanmean(im.data[0])>1e3:
        im = im / im_scalar
    # replace no data values with NaN
    im = im.where(im!=-9999)
    # drop uneccesary dimensions
    im = im.squeeze(drop=True)
    # extract bands info
    b = im.data[0].astype(float)
    g = im.data[1].astype(float)
    r = im.data[2].astype(float)
    nir = im.data[3].astype(float)
    
    # -----Load classified image
    im_classified = rxr.open_rasterio(im_classified_path + im_classified_fn)
    # replace no data values with NaN
    im_classified = im_classified.where(im_classified!=-9999)
    # drop uneccesary dimensions
    im_classified = im_classified.squeeze(drop=True)

    # -----Interpolate DEM to image points
    x, y = im_classified.indexes.values() # grab indices of image
    DEM_interp = DEM.interp(x=x, y=y, method="nearest") # interpolate DEM to image coordinates
    DEM_interp_masked = DEM_interp.where(im_classified<=2) # mask image where not classified as snow
    snow_elev = DEM_interp_masked.elevation.data.flatten() # create array of snow elevations
    snow_elev = np.sort(snow_elev[~np.isnan(snow_elev)]) # sort and remove NaNs
    
    # minimum elevation of the image where data exist
    im_elev_min = np.nanmin(DEM_interp.elevation.data.flatten())
    im_elev_max = np.nanmax(DEM_interp.elevation.data.flatten())
    
    # plot snow elevations histogram
    if plot_output:
        fig, ax, sl_points_AOI = plot_im_classified_histogram_contour(im, im_classified, DEM, AOI, None)
        return im_elev_min, im_elev_max, snow_elev, fig
        
    return im_elev_min, im_elev_max, snow_elev

# --------------------------------------------------
def reduce_memory_usage(df, verbose=True):
# from Bex T (2021): https://towardsdatascience.com/6-pandas-mistakes-that-silently-tell-you-are-a-rookie-b566a252e60d
    numerics = ["int8", "int16", "int32", "int64", "float16", "float32", "float64"]
    start_mem = df.memory_usage().sum() / 1024 ** 2
    for col in df.columns:
        col_type = df[col].dtypes
        if col_type in numerics:
            c_min = df[col].min()
            c_max = df[col].max()
            if str(col_type)[:3] == "int":
                if c_min > np.iinfo(np.int8).min and c_max < np.iinfo(np.int8).max:
                    df[col] = df[col].astype(np.int8)
                elif c_min > np.iinfo(np.int16).min and c_max < np.iinfo(np.int16).max:
                    df[col] = df[col].astype(np.int16)
                elif c_min > np.iinfo(np.int32).min and c_max < np.iinfo(np.int32).max:
                    df[col] = df[col].astype(np.int32)
                elif c_min > np.iinfo(np.int64).min and c_max < np.iinfo(np.int64).max:
                    df[col] = df[col].astype(np.int64)
            else:
#                if (
#                    c_min > np.finfo(np.float16).min
#                    and c_max < np.finfo(np.float16).max
#                ):
#                    df[col] = df[col].astype(np.float16) # float16 not compatible with linalg
                if (#elif (
                    c_min > np.finfo(np.float32).min
                    and c_max < np.finfo(np.float32).max
                ):
                    df[col] = df[col].astype(np.float32)
                else:
                    df[col] = df[col].astype(np.float64)
    end_mem = df.memory_usage().sum() / 1024 ** 2
    if verbose:
        print(
            "Mem. usage decreased to {:.2f} Mb ({:.1f}% reduction)".format(
                end_mem, 100 * (start_mem - end_mem) / start_mem
            )
        )
    return df
    
# --------------------------------------------------
def convert_wgs_to_utm(lon: float, lat: float):
    """Based on lat and lon, return best utm epsg-code"""
    utm_band = str((math.floor((lon + 180) / 6 ) % 60) + 1)
    if len(utm_band) == 1:
        utm_band = '0'+utm_band
    if lat >= 0:
        epsg_code = '326' + utm_band
        return epsg_code
    epsg_code = '327' + utm_band
    return epsg_code
