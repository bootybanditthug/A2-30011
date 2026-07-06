import json
import threading

import mysql.connector
import serial
from flask import Flask, redirect, render_template, request, url_for

# --- Configuration ---------------------------------------------------------
SERIAL_PORT = '/dev/ttyACM0'
BAUD_RATE = 9600

DB_CONFIG = {
    'host': 'localhost',
    'user': 'hawk267',
    'password': 'Ytho191205@',  # match the user created in raspberry-pi/README.md
    'database': 'smarthome',
}

# Task #4: simple edge conditional rule - sustained close proximity.
# Requires several consecutive close readings (not a single noisy one) -
# the same "don't act on one noisy sample" lesson learned on the Arduino
# side already.
PROXIMITY_ALERT_CM = 10
PROXIMITY_ALERT_STREAK = 3

# --- Shared state, updated by the background serial reader -----------------
app = Flask(__name__)
arduino = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)

state_lock = threading.Lock()
latest = {
    'light_level': 0,
    'distance': 0,
    'mode': 'AUTO',
    'light': False,
}

close_streak = 0


def store_reading(data):
    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        cursor = conn.cursor()
        cursor.execute(
            'INSERT INTO lighting (mode, light, light_level, distance) '
            'VALUES (%s, %s, %s, %s)',
            (data['mode'], data['light'], data['light_level'], data['distance']),
        )
        conn.commit()
        cursor.close()
        conn.close()
    except mysql.connector.Error as e:
        print(f'Database error: {e}')


def fetch_analysis(limit=50):
    # Task #5 "extend with analysis (mean, min-max, etc.)" - simple SQL
    # aggregates over the most recent readings.
    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            'SELECT AVG(light_level) AS avg_light, MIN(light_level) AS min_light, '
            'MAX(light_level) AS max_light, AVG(distance) AS avg_distance, '
            'MIN(distance) AS min_distance, MAX(distance) AS max_distance '
            'FROM (SELECT * FROM lighting ORDER BY time DESC LIMIT %s) AS recent',
            (limit,),
        )
        row = cursor.fetchone()
        cursor.close()
        conn.close()
        return row
    except mysql.connector.Error as e:
        print(f'Database error: {e}')
        return None


def evaluate_rule(data):
    # Task #4: notification always fires on sustained close proximity;
    # actually operating the light is scoped to Manual Mode only, so this
    # rule never fights the Arduino's own autonomous Auto Mode decision.
    global close_streak

    if data['distance'] < PROXIMITY_ALERT_CM:
        close_streak += 1
    else:
        close_streak = 0

    if close_streak == PROXIMITY_ALERT_STREAK:
        print(f"[ALERT] Sustained close proximity detected ({data['distance']}cm)")
        if data['mode'] == 'MANUAL':
            arduino.write(b'LED_ON\n')


def serial_reader():
    while True:
        line = arduino.readline().decode('utf-8', errors='ignore').strip()
        if not line:
            continue

        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue

        if not all(k in data for k in ('light_level', 'distance', 'mode', 'light')):
            continue

        with state_lock:
            latest.update(data)

        store_reading(data)
        evaluate_rule(data)


# --- Routes ------------------------------------------------------------

@app.route('/')
def index():
    with state_lock:
        data = dict(latest)
    data['analysis'] = fetch_analysis()
    return render_template('index.html', data=data)


@app.route('/toggle-mode')
def toggle_mode():
    with state_lock:
        going_auto = latest['mode'] != 'AUTO'
    arduino.write(b'AUTO\n' if going_auto else b'MANUAL\n')
    return redirect(url_for('index'))


@app.route('/toggle-led')
def toggle_led():
    # Only meaningful in Manual Mode - the light is sensor-driven in Auto Mode.
    with state_lock:
        currently_on = latest['light']
    arduino.write(b'LED_OFF\n' if currently_on else b'LED_ON\n')
    return redirect(url_for('index'))


@app.route('/set-light-threshold', methods=['POST'])
def set_light_threshold():
    value = request.form.get('light_threshold', '').strip()
    if value.isdigit():
        arduino.write(f'LIGHT_TH:{value}\n'.encode('utf-8'))
    return redirect(url_for('index'))


@app.route('/set-distance-threshold', methods=['POST'])
def set_distance_threshold():
    value = request.form.get('distance_threshold', '').strip()
    if value.isdigit():
        arduino.write(f'DIST_TH:{value}\n'.encode('utf-8'))
    return redirect(url_for('index'))


if __name__ == '__main__':
    threading.Thread(target=serial_reader, daemon=True).start()
    # debug=False on purpose: Flask's debug reloader re-executes this script in
    # a second process, which would open the serial port twice and crash.
    app.run(host='0.0.0.0', port=8080, debug=False)
