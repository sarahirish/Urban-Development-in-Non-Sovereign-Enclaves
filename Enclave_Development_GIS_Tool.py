import arcpy
import os
import random

def run_analysis():
    # Inputs
    boundary_file = arcpy.GetParameterAsText(0)
    target_gdb = arcpy.GetParameterAsText(1)
    dem_file = arcpy.GetParameterAsText(2)
    buffer_dist = arcpy.GetParameterAsText(3)
    master_crs = arcpy.GetParameterAsText(4)
    project_root = arcpy.GetParameterAsText(5)
    from_raster = arcpy.GetParameterAsText(6)
    to_raster = arcpy.GetParameterAsText(7)

    # Set up environments
    arcpy.env.overwriteOutput = True
    arcpy.env.workspace = target_gdb
    arcpy.env.outputCoordinateSystem = master_crs
    arcpy.CheckOutExtension("Spatial")
    arcpy.CheckOutExtension("ImageAnalyst")

    area = os.path.basename(boundary_file).split("_")[0]
    snap_raster_path = from_raster

    # Create subfolders for each variable
    subfolders = ["Distance", "LandChange", "Status", "Slope"]
    paths = {f: os.path.join(project_root, f) for f in subfolders}
    for p in paths.values():
        if not os.path.exists(p): os.makedirs(p)

    arcpy.AddMessage(f"Starting analysis for: {area}")

    # Buffer
    buffer_fc = os.path.join(target_gdb, f"{area}_Buffer")
    arcpy.analysis.PairwiseBuffer(boundary_file, buffer_fc, buffer_dist)


    # Enclave Status Raster
    ident = arcpy.analysis.Identity(buffer_fc, boundary_file, "memory/ident")
    status_rast = os.path.join(paths["Status"], f"{area}_Status.tif")
    field_name = arcpy.ListFields(ident, "*OBJECTID*")[0].name
    with arcpy.EnvManager(cellSize=10, mask=buffer_fc, snapRaster=snap_raster_path, extent=buffer_fc):
        arcpy.conversion.FeatureToRaster(ident, field_name, status_rast)

    # Slope and Buildable Raster
    slope_path = os.path.join(paths["Slope"], f"{area}_Slope.tif")
    buildable_path = os.path.join(paths["Slope"], f"{area}_Buildable.tif")
    with arcpy.EnvManager(cellSize=10, mask=buffer_fc, snapRaster=snap_raster_path, extent=buffer_fc):
        slope_obj = arcpy.sa.SurfaceParameters(dem_file, "SLOPE", "QUADRATIC", "DEGREE")
        slope_obj.save(slope_path)
        remap = arcpy.sa.RemapRange([[0, 15, 1], [15, 90, 0]])
        build_raster = arcpy.sa.Reclassify(slope_path, "VALUE", remap, "NODATA")
        build_raster.save(buildable_path)

    # Distance to Border
    dist_path = os.path.join(paths["Distance"], f"{area}_Distance.tif")
    with arcpy.EnvManager(cellSize=10, mask=buffer_fc, snapRaster=snap_raster_path, extent=buffer_fc):
        b_line = arcpy.management.FeatureToLine(boundary_file, "memory/line")
        dist_accum = arcpy.sa.DistanceAccumulation(b_line)
        dist_accum.save(dist_path)

    # Land Change Analysis
    land_change_path = os.path.join(paths["LandChange"], f"{area}_LandChange.tif")
    binary_change_path = os.path.join(paths["LandChange"], f"{area}_Change_Binary.tif")
    from_classes = ["12", "13", "14", "21", "22", "23", "24", "31", "32"]
    with arcpy.EnvManager(cellSize=10, mask=buffer_fc, snapRaster=snap_raster_path, extent=buffer_fc):
        change_rast = arcpy.ia.ComputeChangeRaster(from_raster, to_raster, "CATEGORICAL_DIFFERENCE", from_classes, ["11"], "CHANGED_PIXELS_ONLY")
        change_rast.save(land_change_path)
        max_val = float(arcpy.management.GetRasterProperties(land_change_path, "MAXIMUM").getOutput(0))
        binary_rast = arcpy.sa.Con(arcpy.sa.Raster(land_change_path) != max_val, 1, 0)
        binary_rast.save(binary_change_path)

    # Create points from raster
    training_points = os.path.join(target_gdb, f"{area}_Points")
    with arcpy.EnvManager(cellSize=10, mask=buffer_fc, snapRaster=snap_raster_path, extent=buffer_fc):
        arcpy.conversion.RasterToPoint(status_rast, training_points, "VALUE")
        arcpy.management.AlterField(training_points, "grid_code", "Status", "Status")

    # Make sure there are coordinates in points feature class

    arcpy.management.AddXY(training_points)
    arcpy.management.AlterField(training_points, "POINT_X", "X_Coord", "X_Coord")
    arcpy.management.AlterField(training_points, "POINT_Y", "Y_Coord", "Y_Coord")

    # Add values from other rasters to points
    extraction_list = [
        [dist_path, "Distance"],
        [binary_change_path, "Change"],
        [buildable_path, "Buildable"],
        [slope_path, "Slope"]
    ]
    arcpy.sa.ExtractMultiValuesToPoints(training_points, extraction_list, "NONE")

    with arcpy.da.UpdateCursor(training_points, ["Change"]) as cursor:
        for row in cursor:
            if row[0] is None:
                row[0] = 0
                cursor.updateRow(row)

    # Use a subset of points that didn't change in a more balanced dataset
    points_data = [(r[0], r[1]) for r in arcpy.da.SearchCursor(training_points, ["OID@", "Change"])]
    growth_ids = [p[0] for p in points_data if p[1] == 1]
    no_growth_ids = [p[0] for p in points_data if p[1] == 0]

    if not growth_ids:
        arcpy.AddWarning(f"No growth (Change=1) detected for {area}. Balanced dataset will not be created.")
    else:
        sample_size = min(len(growth_ids), len(no_growth_ids))
        final_ids = growth_ids + random.sample(no_growth_ids, sample_size)

        balanced_fc = os.path.join(target_gdb, f"{area}_points_balanced")
        oid_fld = arcpy.Describe(training_points).OIDFieldName
        where = f"{oid_fld} IN ({','.join(map(str, final_ids))})"

        arcpy.management.MakeFeatureLayer(training_points, "temp_lyr", where)
        arcpy.management.CopyFeatures("temp_lyr", balanced_fc)

    # Area tabulations
    zonal_table = os.path.join(target_gdb, f"{area}_Tab")
    arcpy.sa.TabulateArea(status_rast, "Value", binary_change_path, "Value", zonal_table, 10, "CLASSES_AS_ROWS")

if __name__ == '__main__':
    run_analysis()