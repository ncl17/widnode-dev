import numpy as np

def save_array_to_file(file_name, data_array):
    import json
    try:
        # Convertir el array a una cadena de texto (JSON)
        json_string = json.dumps(data_array)

        # Ruta del archivo en la carpeta de documentos (cambia el directorio según necesites)
        path = f"{file_name}.json"

        # Escribir el archivo
        with open(path, 'w') as file:
            file.write(json_string)

        print(f"Archivo guardado en: {path}")
    except Exception as error:
        print(f"Error al guardar el archivo: {error}")

def descomponer_medicion(medicion):
    import numpy as np
    serie_x = []
    serie_y = []
    serie_z = []

    rmsx = 0
    rmsy = 0
    rmsz = 0

    # print(len(medicion))

    for i in range(len(medicion) // 6):
        x = int.from_bytes(medicion[i*6:i*6+2], byteorder='little', signed=True) * 0.00048828125
        y = int.from_bytes(medicion[i*6+2:i*6+4], byteorder='little', signed=True) * 0.00048828125
        z = int.from_bytes(medicion[i*6+4:i*6+6], byteorder='little', signed=True) * 0.00048828125

        rmsx += x ** 2
        rmsy += y ** 2
        rmsz += z ** 2

        serie_x.append(x)
        serie_y.append(y)
        serie_z.append(z)

    rmsx = (rmsx / (len(medicion) // 6)) ** 0.5
    rmsy = (rmsy / (len(medicion) // 6)) ** 0.5
    rmsz = (rmsz / (len(medicion) // 6)) ** 0.5

    fftx = np.abs(np.fft.fft(serie_x))[1:]  # Ignorar el primer elemento
    ffty = np.abs(np.fft.fft(serie_y))[1:]  # Ignorar el primer elemento
    fftz = np.abs(np.fft.fft(serie_z))[1:]  # Ignorar el primer elemento

    return {
        "serieX": serie_x,
        "serieY": serie_y,
        "serieZ": serie_z,
        "rmsX": rmsx,
        "rmsY": rmsy,
        "rmsZ": rmsz,
        "fftX": fftx.tolist(),
        "fftY": ffty.tolist(),
        "fftZ": fftz.tolist(),
    }

def _global_features(arr: np.ndarray):
    # arr en g (como en el sensor con aRes aplicado)
    arr = np.asarray(arr, dtype=np.float64)
    n = arr.size
    if n == 0:
        return dict(mean=0.0, rms=0.0, skew=0.0, kurt=0.0,
                    ptp=0.0, crest=0.0, shape=0.0, impulse=0.0)

    mean = float(np.mean(arr))
    rms  = float(np.sqrt(np.mean(arr**2)))
    abs_mean = float(np.mean(np.abs(arr)))
    peak = float(np.max(np.abs(arr)))
    ptp  = float(np.max(arr) - np.min(arr))
    std  = float(np.std(arr, ddof=0))

    # Skewness y kurtosis "clásicas" (sin restar 3 a la kurtosis)
    if std > 0:
        z = (arr - mean) / std
        skew = float(np.mean(z**3))
        kurt = float(np.mean(z**4))
    else:
        skew = 0.0
        kurt = 0.0

    crest   = float(peak / rms) if rms > 0 else 0.0
    shape   = float(rms / abs_mean) if abs_mean > 0 else 0.0
    impulse = float(peak / abs_mean) if abs_mean > 0 else 0.0

    return dict(mean=mean, rms=rms, skew=skew, kurt=kurt,
                ptp=ptp, crest=crest, shape=shape, impulse=impulse)

def descomponer_medicion_ext(medicion,drop_bins: int = 5):
    import numpy as np
    import logging
    sample_rate = 26667
    serie_x = []
    serie_y = []
    serie_z = []

    rmsx = 0
    rmsy = 0
    rmsz = 0

    # print(len(medicion))

    for i in range(len(medicion) // 6):
        x = int.from_bytes(medicion[i*6:i*6+2], byteorder='little', signed=True) * 0.00048828125
        y = int.from_bytes(medicion[i*6+2:i*6+4], byteorder='little', signed=True) * 0.00048828125
        z = int.from_bytes(medicion[i*6+4:i*6+6], byteorder='little', signed=True) * 0.00048828125

        serie_x.append(x)
        serie_y.append(y)
        serie_z.append(z)

    N = 8192

    # Usamos exactamente N como en el sensor
    serie_xb = serie_x
    serie_yb = serie_y
    serie_zb = serie_z  

    serie_x = serie_x[:N]
    serie_y = serie_y[:N]
    serie_z = serie_z[:N]

    signal_size = N // 2
    fs = 26667

    
    
    # Bloques
    b1x, b2x = serie_x[:signal_size], serie_x[signal_size:N]
    b1y, b2y = serie_y[:signal_size], serie_y[signal_size:N]
    b1z, b2z = serie_z[:signal_size], serie_z[signal_size:N]


    # rFFT
    f1x = np.fft.rfft(np.asarray(b1x, dtype=np.float32))
    f2x = np.fft.rfft(np.asarray(b2x, dtype=np.float32))
    f1y = np.fft.rfft(np.asarray(b1y, dtype=np.float32))
    f2y = np.fft.rfft(np.asarray(b2y, dtype=np.float32))
    f1z = np.fft.rfft(np.asarray(b1z, dtype=np.float32))
    f2z = np.fft.rfft(np.asarray(b2z, dtype=np.float32))

    # promedio de magnitudes como en C
    magx = (np.abs(f1x) + np.abs(f2x)) * 0.5
    magy = (np.abs(f1y) + np.abs(f2y)) * 0.5
    magz = (np.abs(f1z) + np.abs(f2z)) * 0.5 

    scale_factor = 2.0 / signal_size

    # === (2) quitar DC..(drop_bins-1) y también Nyquist para replicar el for del C ===
    # rfftfreq devuelve [0 .. signal_size/2] inclusive
    freqs_full = np.fft.rfftfreq(signal_size, d=1.0/fs)
    # índice máximo a incluir (excluyendo Nyquist)
    last = (signal_size // 2)  # índice de Nyquist
    sl = slice(drop_bins, last)  # [drop_bins .. last-1] igual que C

    fftX = (magx[sl] * scale_factor).astype(np.float64)
    fftY = (magy[sl] * scale_factor).astype(np.float64)
    fftZ = (magz[sl] * scale_factor).astype(np.float64)
    freqs = freqs_full[sl]
    
    # RMS espectral (aceleración, en g)
    rmsX = float(np.sqrt(np.sum(fftX**2)))
    rmsY = float(np.sqrt(np.sum(fftY**2)))
    rmsZ = float(np.sqrt(np.sum(fftZ**2)))
    
    # Velocidad: a[g] -> a[m/s^2] -> v = a / (2πf) -> mm/s
    g_to_mss = 9.80665
    velX_spec = (fftX * g_to_mss) / (2.0 * np.pi * freqs) * 1000.0
    velY_spec = (fftY * g_to_mss) / (2.0 * np.pi * freqs) * 1000.0
    velZ_spec = (fftZ * g_to_mss) / (2.0 * np.pi * freqs) * 1000.0

    rmsX_vel = float(np.sqrt(np.sum(velX_spec**2)))
    rmsY_vel = float(np.sqrt(np.sum(velY_spec**2)))
    rmsZ_vel = float(np.sqrt(np.sum(velZ_spec**2)))


    # Globales en tiempo (como en el sensor) sobre N muestras
    gx = _global_features(np.asarray(serie_x))
    gy = _global_features(np.asarray(serie_y))
    gz = _global_features(np.asarray(serie_z))

    # rmsx_vel = np.sqrt(np.sum(np.square(fftx_vel)))
    # rmsy_vel = np.sqrt(np.sum(np.square(ffty_vel)))
    # rmsz_vel = np.sqrt(np.sum(np.square(fftz_vel)))
    

    return {
        # series completas (g)
        "serieX": serie_xb,
        "serieY": serie_yb,
        "serieZ": serie_zb,

        # espectral (g) y (mm/s)
        "fftX": fftX.tolist(),
        "fftY": fftY.tolist(),
        "fftZ": fftZ.tolist(),
        "rmsX": rmsX,
        "rmsY": rmsY,
        "rmsZ": rmsZ,
        "rmsX_vel": rmsX_vel,
        "rmsY_vel": rmsY_vel,
        "rmsZ_vel": rmsZ_vel,

        # globales tiempo por eje (g)
        "mediaX": gx["mean"], "rmsAxisX": gx["rms"], "skewX": gx["skew"], "kurtosisX": gx["kurt"],
        "ptpX": gx["ptp"], "crestFactorX": gx["crest"], "shapeFactorX": gx["shape"], "impulseFactorX": gx["impulse"],

        "mediaY": gy["mean"], "rmsAxisY": gy["rms"], "skewY": gy["skew"], "kurtosisY": gy["kurt"],
        "ptpY": gy["ptp"], "crestFactorY": gy["crest"], "shapeFactorY": gy["shape"], "impulseFactorY": gy["impulse"],

        "mediaZ": gz["mean"], "rmsAxisZ": gz["rms"], "skewZ": gz["skew"], "kurtosisZ": gz["kurt"],
        "ptpZ": gz["ptp"], "crestFactorZ": gz["crest"], "shapeFactorZ": gz["shape"], "impulseFactorZ": gz["impulse"],
    }

# def descomponer_medicion_ext(medicion):
#     import numpy as np
#     import logging
#     sample_rate = 26667
#     serie_x = []
#     serie_y = []
#     serie_z = []

#     rmsx = 0
#     rmsy = 0
#     rmsz = 0

#     # print(len(medicion))

#     for i in range(len(medicion) // 6):
#         x = int.from_bytes(medicion[i*6:i*6+2], byteorder='little', signed=True) * 0.00048828125
#         y = int.from_bytes(medicion[i*6+2:i*6+4], byteorder='little', signed=True) * 0.00048828125
#         z = int.from_bytes(medicion[i*6+4:i*6+6], byteorder='little', signed=True) * 0.00048828125

#         serie_x.append(x)
#         serie_y.append(y)
#         serie_z.append(z)

#     # rmsx = (rmsx / (len(medicion) // 6)) ** 0.5
#     # rmsy = (rmsy / (len(medicion) // 6)) ** 0.5
#     # rmsz = (rmsz / (len(medicion) // 6)) ** 0.5
#     # Longitud de la señal
#     # Longitud de la señal
#     N = len(serie_x[:8192])

#     # Calcular la FFT y normalizar (frecuencias positivas)
#     fftx = np.abs(np.fft.fft(serie_x[:8192])[:N // 2]) * (2 / N)
#     ffty = np.abs(np.fft.fft(serie_y[:8192])[:N // 2]) * (2 / N)
#     fftz = np.abs(np.fft.fft(serie_z[:8192])[:N // 2]) * (2 / N)

#     fftx=fftx[5:]
#     ffty=ffty[5:]
#     fftz=fftz[5:]

#     # Calcular el RMS de las FFT
    
#     rmsx = np.sqrt(np.sum(np.square(fftx)))
#     rmsy = np.sqrt(np.sum(np.square(ffty)))
#     rmsz = np.sqrt(np.sum(np.square(fftz)))

#     # logging.info(f"RMS FFT X: {rmsx}, Y: {rmsy}, Z: {rmsz}")

#     # Frecuencias asociadas
#     freq = np.fft.fftfreq(N, d=1/sample_rate)[:N // 2]
#     freq = freq[5:]

#     # Evitar la división por cero (frecuencia DC)
#     # freq[0] = np.inf  # Evitar división por cero en la frecuencia

#     # Cálculo de la FFT de la velocidad
#     fftx_vel = np.abs(fftx / (2 * np.pi * freq))* 1000 * 9.80665
#     ffty_vel = np.abs(ffty / (2 * np.pi * freq))* 1000 * 9.80665
#     fftz_vel = np.abs(fftz / (2 * np.pi * freq))* 1000 * 9.80665

#     # Restaurar la frecuencia DC (lo dejamos como cero para resultados claros)
#     # freq[0] = 0

#     # RMS en velocidad


#     rmsx_vel = np.sqrt(np.sum(np.square(fftx_vel)))
#     rmsy_vel = np.sqrt(np.sum(np.square(ffty_vel)))
#     rmsz_vel = np.sqrt(np.sum(np.square(fftz_vel)))
    

#     return {
#         "serieX": serie_x,
#         "serieY": serie_y,
#         "serieZ": serie_z,
#         "rmsX": rmsx,
#         "rmsY": rmsy,
#         "rmsZ": rmsz,
#         "fftX": fftx.tolist(),
#         "fftY": ffty.tolist(),
#         "fftZ": fftz.tolist(),
#         "rmsX_vel": rmsx_vel,
#         "rmsY_vel": rmsy_vel,
#         "rmsZ_vel": rmsz_vel,
#     }