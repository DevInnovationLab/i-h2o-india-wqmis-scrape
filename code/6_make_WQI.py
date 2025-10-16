import pandas as pd
import os
import numpy as np
from scipy.interpolate import interp1d

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

def make_wqi():
    pqs = [f for f in os.listdir(intermediate_path) if f.endswith('.parquet')]
    for pq in pqs:
        df = pd.read_parquet(os.path.join(intermediate_path,pq))
        
        # Apply to create a new column
        df['QuarterStart'] = df['Sample tested date'].apply(get_quarter_start)

        # Replace -1.0 with NaN so they are excluded from the mean
        df_clean = df.copy()
        for col in ['Fluoride', 'Nitrate', 'EColi']:
            df_clean[col] = df_clean[col].replace(-1.0, np.nan)

        # Group by quarter and calculate the mean
        agg_df = df_clean.groupby(['District','Village','QuarterStart'])[['Fluoride', 'Nitrate', 'EColi']].max().reset_index().fillna(-1)

        # Make subindices
        agg_df['Fluoride_Subindex'] = fluoride_subindex(agg_df['Fluoride'])
        agg_df['EColi_Subindex'] = ecoli_subindex(agg_df['EColi'])

        # Drop if EColi is 0 
        filtered_df = agg_df[agg_df['EColi_Subindex'] > 0].copy()

        # Step 1: Compute the max subindex per row
        filtered_df['WQI'] = filtered_df[['EColi_Subindex', 'Fluoride_Subindex']].max(axis=1)

        # Step 2: Identify the max subindex *within each group*
        filtered_df['Group_Key'] = filtered_df[['State (2024-2025)','District', 'Village', 'QuarterStart']].astype(str).agg('|'.join, axis=1)
        max_per_group = filtered_df.groupby('Group_Key')['WQI'].transform('max')

        # Step 3: Filter rows where the row-level Max_Subindex equals the group max
        filtered_df = filtered_df[filtered_df['WQI'] == max_per_group].copy()

        # (Optional) Drop the helper Group_Key column
        filtered_df.drop(columns='Group_Key', inplace=True)
        
        print(filtered_df.tail(100))

get_clean_save()
make_wqi()