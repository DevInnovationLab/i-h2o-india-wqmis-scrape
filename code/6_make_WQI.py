import pandas as pd
import os
import numpy as np
from scipy.interpolate import interp1d
import geopandas as gpd
import folium
from branca.colormap import LinearColormap

folder_path = '/Users/admin/Library/CloudStorage/Box-Box/India WQMIS Data/Format L2/Processed Databases'
intermediate_path = '/Users/admin/Library/CloudStorage/Box-Box/India WQMIS Data/Format L2/Intermediate for WQI'

def get_clean_save():
# Get list of csvs, clean and save to parquet
    csvs = [f for f in os.listdir(folder_path) if f.endswith('.csv')]
    for file in csvs:
        df = pd.read_csv(os.path.join(folder_path,file))
        df = df[['State (2024-2025)','District','Village','Chemical parameters','Bacteriologial parameters','Sample tested date']]
        #df = df.head(100)
        df['Sample tested date'] = pd.to_datetime(df['Sample tested date'],format="%d/%m/%Y")
        df['Fluoride'] = df['Chemical parameters'].str.extract(r'Fluoride \(as F\) \(mg/l\) : ([0-9\.]+)')
        df['Fluoride'] = pd.to_numeric(df['Fluoride'], errors='coerce')
        df['Nitrate'] = df['Chemical parameters'].str.extract(r'Nitrate \(as NO3\) \(mg/l\) : ([0-9\.]+)')
        df['Nitrate'] = pd.to_numeric(df['Nitrate'], errors='coerce')
        df['EColi'] = df['Bacteriologial parameters'].str.extract(r'E\. coli \(CFU/100 ml\) : ([0-9\.]+)')
        df['EColi'] = pd.to_numeric(df['EColi'], errors='coerce')
        df.fillna(-1,inplace=True)
        df = df[['State (2024-2025)','District','Village','Sample tested date','Fluoride','Nitrate','EColi']]
        df.to_parquet(os.path.join(intermediate_path,file.replace('.csv','.parquet')))

def get_quarter_start(date):
    quarter = (date.month - 1) // 3
    return pd.Timestamp(year=date.year, month=quarter * 3 + 1, day=1)

# Fluoride subindex function
def fluoride_subindex(x):
    # Data points
    # Fluoride in mg/L
    fluoride_levels = np.array([0.2,0.5,1,1.2,1.5,1.7,2,2.5,3,3.26])
    subindex_values = np.array([0.0,0.0,0.0,7.9,21.4,30.3,43.7,66.0,88.4,100.0])
    # Create a cubic spline interpolator
    interp_func = interp1d(fluoride_levels, subindex_values, kind='cubic', fill_value='extrapolate')

    # Initialize output array
    result = np.empty_like(x, dtype=float)

    # Apply clipping logic
    result[x < fluoride_levels.min()] = 0
    result[x > fluoride_levels.max()] = 100
    in_range = (x >= fluoride_levels.min()) & (x <= fluoride_levels.max())
    result[in_range] = interp_func(x[in_range])

    return result

# E. coli subindex function
def ecoli_subindex(x):
    # Sample data points: (E. coli count, Subindex value)
    ecoli_counts = np.array([1.000,1.585,2.512,3.981,6.310,10.000,15.849,25.119,39.811,63.096,100.000])
    subindex_values = np.array([0,10,20,30,40,50,60,70,80,90,100])
    
    interp_func = interp1d(ecoli_counts, subindex_values, kind='cubic', fill_value='extrapolate')

    # Initialize output array
    result = np.empty_like(x, dtype=float)

    # Apply clipping logic
    result[x <= ecoli_counts.min()] = 0
    result[x >= ecoli_counts.max()] = 100
    in_range = (x >= ecoli_counts.min()) & (x <= ecoli_counts.max())
    result[in_range] = interp_func(x[in_range])

    return result
def make_wqi_with_geometry():
    # Load geojsons
    shapefile_path = '/Users/admin/Library/CloudStorage/Box-Box/India WQMIS Data/Shapefiles'
    gdf1 = gpd.read_file(os.path.join(shapefile_path,'mh1.geojson'))
    gdf2 = gpd.read_file(os.path.join(shapefile_path,'mh2.geojson'))
    gdf = pd.concat([gdf1, gdf2], ignore_index=True)
    
    # Normalize for matching
    gdf['district_norm'] = gdf['DISTRICT'].str.strip().str.upper()
    gdf['village_norm'] = gdf['NAME'].str.strip().str.upper()
    
    # Process WQI data
    pqs = [f for f in os.listdir(intermediate_path) if f.endswith('.parquet')]
    all_data = []
    
    for pq in pqs:
        df = pd.read_parquet(os.path.join(intermediate_path, pq))
        df['QuarterStart'] = df['Sample tested date'].apply(get_quarter_start)

        df_clean = df.copy()
        for col in ['Fluoride', 'EColi']:
            df_clean.loc[df_clean[col] == -1.0, col] = np.nan

        # Group by quarter: max contaminant per village-quarter
        agg_df = df_clean.groupby(['State (2024-2025)','District','Village','QuarterStart'])[['Fluoride', 'EColi']].max().reset_index()
        
        # Require both measurements
        agg_df = agg_df[agg_df['Fluoride'].notna() & agg_df['EColi'].notna()].copy()

        # Compute subindices
        agg_df['Fluoride_Subindex'] = fluoride_subindex(agg_df['Fluoride'].values)
        agg_df['EColi_Subindex'] = ecoli_subindex(agg_df['EColi'].values)
        
        agg_df['WQI'] = agg_df[['Fluoride_Subindex', 'EColi_Subindex']].max(axis=1)
        
        all_data.append(agg_df)
    
    combined = pd.concat(all_data, ignore_index=True)
    
    # Aggregate to village level: max WQI and corresponding values
    village_wqi = combined.loc[combined.groupby(['State (2024-2025)','District','Village'])['WQI'].idxmax()]
    village_wqi = village_wqi[['State (2024-2025)','District','Village','WQI','Fluoride','Fluoride_Subindex','EColi','EColi_Subindex']].reset_index(drop=True)
    
    # Normalize for matching
    village_wqi['district_norm'] = village_wqi['District'].str.strip().str.upper()
    village_wqi['village_norm'] = village_wqi['Village'].str.strip().str.upper()
    
    # Merge
    merged = gdf.merge(
        village_wqi,
        on=['district_norm', 'village_norm'],
        how='left'
    )
    
    print(f"Total villages in geojson: {len(gdf)}")
    print(f"Villages with WQI data: {merged['WQI'].notna().sum()}")
    print(f"Match rate: {merged['WQI'].notna().sum() / len(gdf) * 100:.1f}%")
    
    merged.to_file('intermediate_shapefiles/maharashtra_wqi.geojson', driver='GeoJSON')
    return merged
def create_wqi_map(gdf_wqi, simplify=False, filename='wqi_map.html'):
    gdf_with_data = gdf_wqi[gdf_wqi['WQI'].notna()].copy()
    
    # Simplify geometries if requested
    if simplify:
        gdf_with_data['geometry'] = gdf_with_data['geometry'].simplify(tolerance=0.001)
    
    colormap = LinearColormap(
        colors=['green', 'yellow', 'orange', 'red'],
        vmin=0,
        vmax=100,
        caption='Water Quality Index (WQI)'
    )
    
    m = folium.Map(
        location=[19.7515, 75.7139],
        zoom_start=7,
        tiles='CartoDB positron'
    )
    
    # Create feature groups for filtering
    all_villages = folium.FeatureGroup(name='All Villages (WQI ≥ 0)', show=True)
    wqi_positive = folium.FeatureGroup(name='WQI > 0 only', show=False)
    ecoli_positive = folium.FeatureGroup(name='E.coli > 0 only', show=False)
    
    for idx, row in gdf_with_data.iterrows():
        geojson_obj = folium.GeoJson(
            row['geometry'],
            style_function=lambda x, wqi=row['WQI']: {
                'fillColor': colormap(wqi),
                'color': 'black',
                'weight': 0.5,
                'fillOpacity': 0.7
            },
            tooltip=folium.Tooltip(
                f"<b>{row['NAME']}</b><br>"
                f"District: {row['DISTRICT']}<br>"
                f"<b>WQI: {row['WQI']:.1f}</b><br><br>"
                f"Fluoride: {row['Fluoride']:.2f} mg/L (Subindex: {row['Fluoride_Subindex']:.1f})<br>"
                f"E.coli: {row['EColi']:.1f} CFU/100mL (Subindex: {row['EColi_Subindex']:.1f})"
            )
        )
        
        geojson_obj.add_to(all_villages)
        
        if row['WQI'] > 0:
            folium.GeoJson(
                row['geometry'],
                style_function=lambda x, wqi=row['WQI']: {
                    'fillColor': colormap(wqi),
                    'color': 'black',
                    'weight': 0.5,
                    'fillOpacity': 0.7
                },
                tooltip=folium.Tooltip(
                    f"<b>{row['NAME']}</b><br>"
                    f"District: {row['DISTRICT']}<br>"
                    f"<b>WQI: {row['WQI']:.1f}</b><br><br>"
                    f"Fluoride: {row['Fluoride']:.2f} mg/L (Subindex: {row['Fluoride_Subindex']:.1f})<br>"
                    f"E.coli: {row['EColi']:.1f} CFU/100mL (Subindex: {row['EColi_Subindex']:.1f})"
                )
            ).add_to(wqi_positive)
        
        if row['EColi'] > 0:
            folium.GeoJson(
                row['geometry'],
                style_function=lambda x, wqi=row['WQI']: {
                    'fillColor': colormap(wqi),
                    'color': 'black',
                    'weight': 0.5,
                    'fillOpacity': 0.7
                },
                tooltip=folium.Tooltip(
                    f"<b>{row['NAME']}</b><br>"
                    f"District: {row['DISTRICT']}<br>"
                    f"<b>WQI: {row['WQI']:.1f}</b><br><br>"
                    f"Fluoride: {row['Fluoride']:.2f} mg/L (Subindex: {row['Fluoride_Subindex']:.1f})<br>"
                    f"E.coli: {row['EColi']:.1f} CFU/100mL (Subindex: {row['EColi_Subindex']:.1f})"
                )
            ).add_to(ecoli_positive)
    
    all_villages.add_to(m)
    wqi_positive.add_to(m)
    ecoli_positive.add_to(m)
    
    folium.LayerControl().add_to(m)
    colormap.add_to(m)
    
    m.save(filename)
    print(f"Map saved to {filename} with {len(gdf_with_data)} villages")
    print(f"Villages with WQI > 0: {(gdf_with_data['WQI'] > 0).sum()}")
    print(f"Villages with E.coli > 0: {(gdf_with_data['EColi'] > 0).sum()}")
    return m

# Create both versions
gdf_wqi = make_wqi_with_geometry()
map_complex = create_wqi_map(gdf_wqi, simplify=False, filename='maps/Maharashtra_wqi_map_detailed.html')
map_simple = create_wqi_map(gdf_wqi, simplify=True, filename='maps/Maharashtra_wqi_map_simplified.html')