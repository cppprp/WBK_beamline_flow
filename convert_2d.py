import webknossos as wk
from webknossos import Dataset, BoundingBox, Mag, SamplingModes
import tifffile
import numpy as np
image = tifffile.imread('/Users/asvetlove/Downloads/TolBlue_section_ND13_FS.tif')
print(f"Original shape: {image.shape}")
image_wk = np.transpose(image, (2, 0, 1))  # Now (3, 648, 846)
image_wk = image_wk[..., np.newaxis]
print(f"New shape: {image_wk.shape}")
with wk.webknossos_context(
        token="q4ZFqZNCwGtBh60gdGbpqA",
        url="https://webknossos.embl-hamburg.de"
):
    new_dataset = Dataset("/Users/asvetlove/temp/ND_13_TolBlue", voxel_size= (1000,1000,1), name= "ND_13_TolBlue_w"
    )
    image_layer = new_dataset.add_layer(
        "tol_blue", category= 'color', dtype_per_channel=image_wk.dtype, num_channels=3,
        dataFormat=wk.DataFormat.WKW, bounding_box= BoundingBox(topleft=(0, 0, 0), size=(648, 846, 1))
    )
    image_layer.add_mag(
        "1",
        compress=False,
        chunk_shape=(64, 64, 1),
        chunks_per_shard=(1,1,1)
    ).write(image_wk)
    #mag = Mag(2)
    image_layer.downsample(coarsest_mag=mag, sampling_mode=SamplingModes.CONSTANT_Z)
    uploaded_dataset = new_dataset.upload()


