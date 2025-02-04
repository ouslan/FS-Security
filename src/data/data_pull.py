from sqlmodel import create_engine
from json import JSONDecodeError
from datetime import datetime
import geopandas as gpd
from tqdm import tqdm
import polars as pl
import requests
import logging
import ibis
import os


class DataPull:
    def __init__(
        self,
        database_url: str = "sqlite:///db.sqlite",
        saving_dir: str = "data/",
        update: bool = False,
        debug: bool = False,
        dev: bool = False,
    ) -> None:
        self.debug = debug
        self.saving_dir = saving_dir
        logging.basicConfig(level=logging.INFO)

        # Check if the saving directory exists
        if not os.path.exists(self.saving_dir + "raw"):
            os.makedirs(self.saving_dir + "raw")
            logging.info(f"created the raw folder in {self.saving_dir}")
        if not os.path.exists(self.saving_dir + "processed"):
            os.makedirs(self.saving_dir + "processed")
            logging.info(f"created the processed folder in {self.saving_dir}/processed")
        if not os.path.exists(self.saving_dir + "external"):
            os.makedirs(self.saving_dir + "external")
            logging.info(f"created the external folder in {self.saving_dir}/external")

        self.database_url = database_url
        self.engine = create_engine(self.database_url)
        self.saving_dir = saving_dir
        self.debug = debug
        self.dev = dev
        self.update = update

        if self.database_url.startswith("sqlite"):
            self.conn = ibis.sqlite.connect(self.database_url.replace("sqlite:///", ""))
        elif self.database_url.startswith("postgres"):
            self.conn = ibis.postgres.connect(
                user=self.database_url.split("://")[1].split(":")[0],
                password=self.database_url.split("://")[1].split(":")[1].split("@")[0],
                host=self.database_url.split("://")[1].split(":")[1].split("@")[1],
                port=self.database_url.split("://")[1].split(":")[2].split("/")[0],
                database=self.database_url.split("://")[1].split(":")[2].split("/")[1],
            )
        else:
            raise Exception("Database url is not supported")

    def pull_file(self, url: str, filename: str, verify: bool = True) -> None:
        """
        Pulls a file from a URL and saves it in the filename. Used by the class to pull external files.

        Parameters
        ----------
        url: str
            The URL to pull the file from.
        filename: str
            The filename to save the file to.
        verify: bool
            If True, verifies the SSL certificate. If False, does not verify the SSL certificate.

        Returns
        -------
        None
        """
        chunk_size = 10 * 1024 * 1024
        logging.info(f"started download {filename}")

        with requests.get(url, stream=True, verify=verify) as response:
            total_size = int(response.headers.get("content-length", 0))

            with tqdm(
                total=total_size,
                unit="B",
                unit_scale=True,
                unit_divisor=1024,
                desc="Downloading",
            ) as bar:
                with open(filename, "wb") as file:
                    for chunk in response.iter_content(chunk_size=chunk_size):
                        if chunk:
                            file.write(chunk)
                            bar.update(
                                len(chunk)
                            )  # Update the progress bar with the size of the chunk
        logging.info(f"Succefully downloaded {filename}")

    def pull_query(self, params: list, year: int) -> pl.DataFrame:
        # prepare custom census query
        param = ",".join(params)
        base = "https://api.census.gov/data/"
        flow = "/acs/acs5/profile"
        url = f"{base}{year}{flow}?get={param}&for=county%20subdivision:*&in=state:72&in=county:*"
        df = pl.DataFrame(requests.get(url).json())

        # get names from DataFrame
        names = df.select(pl.col("column_0")).transpose()
        names = names.to_dicts().pop()
        names = dict((k, v.lower()) for k, v in names.items())

        # Pivot table
        df = df.drop("column_0").transpose()
        return df.rename(names).with_columns(year=pl.lit(year))

    def pull_dp03(self) -> ibis.expr.types.relations.Table:
        df = self.conn.table("dp03table")
        for _year in range(2012, datetime.now().year):
            if df.filter(df.year == _year).to_pandas().empty:
                try:
                    logging.info(f"pulling {_year} data")
                    tmp = self.pull_query(
                        params=[
                            "DP03_0051E",
                            "DP03_0052E",
                            "DP03_0053E",
                            "DP03_0054E",
                            "DP03_0055E",
                            "DP03_0056E",
                            "DP03_0057E",
                            "DP03_0058E",
                            "DP03_0059E",
                            "DP03_0060E",
                            "DP03_0061E",
                        ],
                        year=_year,
                    )
                    tmp = tmp.rename(
                        {
                            "dp03_0051e": "total_house",
                            "dp03_0052e": "inc_less_10k",
                            "dp03_0053e": "inc_10k_15k",
                            "dp03_0054e": "inc_15_25k",
                            "dp03_0055e": "inc_25k_35k",
                            "dp03_0056e": "inc_35k_50k",
                            "dp03_0057e": "inc_50k_75k",
                            "dp03_0058e": "inc_75k_100k",
                            "dp03_0059e": "inc_100k_150k",
                            "dp03_0060e": "inc_150k_200k",
                            "dp03_0061e": "inc_more_200k",
                        }
                    )
                    tmp = tmp.with_columns(
                        geoid=pl.col("state")
                        + pl.col("county")
                        + pl.col("county subdivision")
                    ).drop(["state", "county", "county subdivision"])
                    tmp = tmp.with_columns(pl.all().exclude("geoid").cast(pl.Int64))
                    self.conn.insert("dp03table", tmp)
                    logging.info(f"succesfully inserting {_year}")
                except JSONDecodeError:
                    logging.warning(f"The ACS for {_year} is not availabe")
                    continue
            else:
                logging.info(f"data for {_year} is in the database")
                continue
        return self.conn.table("dp03table")

    def pull_shape(self) -> ibis.expr.types.relations.Table:
        if not os.path.exists(f"{self.saving_dir}external/cousub.zip"):
            self.pull_file(
                url="https://www2.census.gov/geo/tiger/TIGER2024/COUSUB/tl_2024_72_cousub.zip",
                filename=f"{self.saving_dir}external/cousub.zip",
            )
        gdf = self.conn.table("geotable")
        if gdf.to_pandas().empty:
            logging.info(
                f"The GeoTable is empty inserting {self.saving_dir}external/cousub.zip"
            )
            tmp = gpd.read_file(f"{self.saving_dir}external/cousub.zip")
            tmp = tmp[["GEOID", "NAME", "geometry"]].rename(
                columns={"GEOID": "geoid", "NAME": "name"}
            )
            tmp.to_postgis("geotable", self.engine, if_exists="append")
            logging.info("Succefully inserting data to database")
        return self.conn.table("geotable")
