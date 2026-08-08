# Lighting Control Overview

The Lighting Control app is a Python-based automation tool that allowa you to control lights (and other switched) devices on a flexible schedule. Device switching is done using the [Smart Device library](https://github.com/NickElseySpelloC/sc-smart-device/) which supports a wide range of smart switch devices. 

## Features
* Lights can be added to groups.
* Multiple schedules supported and multiple on/off events for each schedule .
* Schedules can be flexibly mapped to individual switches, switch groups or a default can be defined.
* Set which days of the week a scheduled event should apply.
* Support for switching on/off at dawn and dusk, including automatic calculation of dawn and dusk times based on location and date. 
* Dawn and dusk times can also include a fixed offet, for example TurnOn: "Dusk+01:15" will turn on the switch a75 minutes before dusk.
* Optionally defined override dates when a schedules should remain off.
* Optionally define "random offsets" which can dynamically adjust on and off times each day.
* Optionally integrate with the [PowerControllerViewer app](https://github.com/NickElseySpelloC/PowerControllerViewer) so that yu can view light status, schedules and history via a web interface.
* Email notification for critical errors.
* Integration with the BetterStack uptime for heatbeat monitoring.

# Installation & Setup
## Prerequisites
* Python 3.x installed:
`brew install python3`
* UV for Python installed:
`brew install uvicorn`

The shell script used to run the app (*scripts/launch.sh*) is uses the *uv sync* command to ensure that all the prerequitie Python packages are installed in the virtual environment.

## Running on Mac
If you're running the Python script on macOS, you need to allow the calling application (Terminal, Visual Studio) to access devices on the local network: *System Settings > Privacy and Security > Local Network*

# Configuration File 
The script uses the *config.yaml* YAML file for configuration. An example of included with the project (*config.yaml.example*). Copy this to *config.yaml* before running the app for the first time.  Here's an example config file:

```yaml
# This is the configuration file for the LightingControl utility 

General:
  # The name of the application
  AppName: My Home Lights
  # Number of seconds to wait before checking the schedules again
  CheckInterval: 60


# Configure the web interface for the LightingControl utility. 
# This is optional but allows you to view the status of the lights and schedules from a web browser. 
# You can also use this to control the lights manually from the web interface.
Website:
  Enable: True
  HostingIP: 0.0.0.0
  Port: 8080
  PageAutoRefresh: 30


# Use this section to configure your Shelly devices used to control the lights
# See this page for more information: https://nickelseyspelloc.github.io/sc-smart-device/
SCSmartDevices:
  ResponseTimeout: 3
  RetryCount: 1
  RetryDelay: 2
  PingAllowed: True
  ShellyWebhooks:
    Enabled: True
    Host: 192.168.86.47
    Port: 8787
    Path: /shelly/webhook
    DefaultWebhooks:
      Inputs:
        - input.toggle_on
        - input.toggle_off
  Devices:
    - Name: Downstairs Lights
      Model: Shelly2PMG3
      Simulate: False
      Inputs:
        - Name: "Living Room Input"
          Webhooks: True
        - Name: "Driveway Input"
          Webhooks: True
      Outputs:
        - Name: "Living Room"
        - Name: "Kitchen"
    - Name: Outside Lights
      Model: Shelly2PMG3
      Simulate: False
      Outputs:
        - Name: "Patio"
          Group: External Lights
        - Name: "Driveway"
          Group: Nighttime


# Use this section to specify the geographic location and timezone of your installation. This is used to determine the dawn and dusk times for lighting control.
# You can do this in one of three ways:
# 1. Use a Google Maps URL to extract the location - specify the GoogleMapsURL field and the Timezone field.
# 2. Manually specify the latitude and longitude - specify the Timezone, Latitude and Longitude fields.
Location:
  Timezone: Europe/Rome
  GoogleMapsURL: https://www.google.com/maps/place/Rome,+Metropolitan+City+of+Rome+Capital/@41.9099533,12.3711975,101027m
  Latitude: 
  Longitude: 


# Maps the schedules to switches and switch groups
LightingControl:
  - Type: Default
    Schedule: Inside Lighting
  - Type: Switch Group
    Target: External Lights
    Schedule: Dusk to Dawn

# Define one or more schedules for controlling the lights
Schedules:
  - Name: Inside Lighting
    Events: 
      - TurnOn: "07:00"
        TurnOff: "09:30"
        DaysOfWeek: Mon,Tue,Wed,Thu,Fri
      - TurnOn: "20:00"
        TurnOff: "23:00"
        DaysOfWeek: All
  - Name: Dusk to Dawn
    Events: 
      - TurnOn: Dusk
        TurnOff: Dawn-01:00
        RandomOffset: 30
        DaysOfWeek: Sat,Sun,Tue
        DatesOff: 
        - StartDate: 2025-09-01
          EndDate: 2025-09-10
  

# Optional. Map Shelly inputs to lighting control actions. If an input is mapped to a switch or switch group 
# then the lights in that target will be On when the input is on, regardless of the schedule 
InputControls:
  - Type: Switch   
    Target: Living Room
    Input: Living Room Input
  - Type: Switch Group
    Target: Nighttime
    Input: Driveway Input


Files:
  # The name of the saved state file. This is used to store the state of the device between runs.
  SavedStateFile: system_state.json
  LogfileName: logs/logfile.log
  LogfileMaxLines: 5000
  # How much information do we write to the log file. One of: none; error; warning; summary; detailed; debug
  LogfileVerbosity: detailed
  # How much information do we write to the console. One of: error; warning; summary; detailed; debug
  ConsoleVerbosity: detailed
  # The state file keeps a log of the switch state changes. This option controls how many days of history we keep. Defaults to 30 days.
  MaxDaysSwitchChangeHistory: 14


# Enter your settings here if you want to be emailed when there's a critical error 
# Set the SMTPUsername and SMTPPassword in your .env file and reference them here for better security
Email:
  EnableEmail: True
  SendEmailsTo: <Your email address here>
  SMTPServer: <Your SMTP server here>
  SMTPPort: 587
  SubjectPrefix: 


ViewerWebsite:
  # Enable or disable posting to the web viewer app
  Enable: True
  # Base URL for the PowerControllerViewer website to post state information to
  BaseURL: http://127.0.0.1:8000
  APITimeout: 20
  # How often to post the state to the web viewer app (in seconds)
  Frequency: 10


# Optionally configure a heartbeat monitor to check the availability of a website - using BetterStack uptime
HeartbeatMonitor:
  # The URL of the website to monitor for availability
  WebsiteURL: https://uptime.betterstack.com/api/v1/heartbeat/myheatbeatid
  # How long to wait for a response from the website before considering it down in seconds
  HeartbeatTimeout: 5
```


## Configuration Parameters
### Section: General

| Parameter | Description | 
|:--|:--|
| AppName | The name for your installation, used in log files and the web app. |
| CheckInterval | How often to check the schedules to see if a switch needs to change. Recommend 60 seconds. |
| WebsiteBaseURL | If you have the PowerControllerViewer web app installed and running (see page 11), then enter the URL for the home page here. Assuming this is on the same machine as this installation, this will typically be http://127.0.0.1:8000. This app uses this URL to pass device state information to the web site. |
| WebsiteAccessKey | If you have configured an access key for the PowerControllerViewer, configure it here. Alternatively, set the VIEWER_ACCESS_KEY environment variable. |
| WebsiteTimeout | How long to wait for a reponse from the PowerControllerViewer when posting state information. |
| DisableAllSwitches | If True, all switches will remain off. It will still be possible to turn the lights on using the web app or a switch input |

### Section: Website

Configure the web interface for the LightingControl utility. This is optional but allows you to view the status of the lights and schedules from a web browser. You can also use this to control the lights manually from the web interface.

| Parameter | Description | 
|:--|:--|
| Enable | Set to True to enable the web app. |
| HostingIP | IP to listen on. Set to 0.0.0.0 to enable all network interfaces on the host. |
| Port | Port to listen on. |
| PageAutoRefresh | Do a full refresh of the webpage (to pick up and configuration changes) every X seconds. |

### Section: SCSmartDevices

In this section you can configure one or more Shelly and/or Tasmota smart switches. See the [Smart Devices library](https://nickelseyspelloc.github.io/sc-smart-device/) for details on how to configure this section.

Webhook call example:

```bash
curl "http://192.168.86.10:8787/shelly/webhook?Event=input.toggle_on&DeviceID=1&ObjectType=input&ComponentID=2"
```

### Section: Location

Use this section to specify the geographic location and timezone of your installation. This is used to determine the dawn and dusk times for lighting control. You can do this in one of three ways:
1. Use a Shelly device's location (using IP lookup)- just specify the device name in the UseShellyDevice field
2. Use a Google Maps URL to extract the location - specify the GoogleMapsURL field and the Timezone field.
3. Manually specify the latitude and longitude - specify the Timezone, Latitude and Longitude fields.

| Parameter | Description | 
|:--|:--|
| UseShellyDevice | The name the Shelly device (configured in the ShellyDevices: Devices section) to use to get your location using IP geo-lookup. |
| Timezone | The timezone of your location. See this link for a list: https://gist.github.com/heyalexej/8bf688fd67d7199be4a1682b3eec7568 |
| GoogleMapsURL | Optionally provide a Google Maps URL which includes the lat/long in the url args. Your location will be determined from this. |
| Latitude | If you haven't provided a Google Maps URL, instead provide the latitude of your location |
| Longitude | If you haven't provided a Google Maps URL, instead provide the lonitude of your location |

### Section: Schedules

Define one or more schedules for controlling the lights

| Parameter | Description | 
|:--|:--|
| Name | The schedule name. Use this name in the LightingControl section below. |
| Events | One or more on/off events for this schedule. See below |

#### Section: Schedules: Events

The table below defines the events part of a schedule. You can have any number of events for each schedule, but you must have at least one.

| Parameter | Description | 
|:--|:--|
| TurnOn | The time to turn the switch on. This can be one of the following formats:<br> - "HH:MM" an explict time in 24 hour format. <br> - "Dawn" turn on at sunrise. <br> - "Dusk" turn on at sunset. <br>Optionally, you can add an offset to the Dawn or Dusk variants, for example: TurnOn: "Dusk+00:20"  will turn on at 20 mins after sunset. |
| TurnOff | The time to turn the switch off. Same formats as above.  |
| RandomOffset | Add or subtract a random number of minutes to the on and off times for this event.  |
| DaysOfWeek | When days of the week should this event apply, for example:<br>DaysOfWeek: Mon,Tue,Wed,Thu,Fri<br>DaysOfWeek: All |
| DatesOff | Optionally, a list of StartDate / StopDate pairs that define which dates this event should be ignores. For example: <br>DatesOff: <br>- StartDate: 2025-09-01<br>&nbsp;&nbsp;EndDate: 2025-09-10 |

### Section: LightingControl

Maps the schedules to switches and switch groups. You can have any number of entries in this section, each one must define at least the Type and Schedule keys.

**Important**: If you are configuring webhooks (in the SCSmartDevices section) so that inputs can control the outout of one of more switch via this app, make sure you have the 'relay type' configured to "Detached" switch mode (in the Shelly app, go to Device > Settings Input/output settings).

| Parameter | Description | 
|:--|:--|
| Type | One of:<br>- Default: Schedule applies to all switches and switch groups not exlictly mapped to schedule elsewhere in this section.<br>- Switch: Map a schedule to a specific switch.<br>- Switch Group: Map a schedule to a specific switch group. |
| Target | The target for this mapping. If Type is Default, leave blank. If Type is Switch, then enter the name of the switch (as defined in ShellyDevices: Devices: Outputs: Name). If Type is Switch Group, then enter the name of the switch group (as defined in ShellyDevices: Devices: Outputs: Group). |
| Schedule | The schedule (as defined in the Schedules section) to map to the default, switch or switch group. |

### Section: InputControls

Optionally map a Shelly relay input to a switch, switch group or all outputs by default. This can be used to over-ride the schedule and turn the lights on regardless. To use this feature you must set the relay type for the Shelly switch to "Detached" switch mode (Settings > Input/output settings in the app)

| Parameter | Description | 
|:--|:--|
| Type | One of:<br>- Default: Input controls all switches and switch groups not exlictly mapped to schedule elsewhere in this section. You don't need to have a default. <br>- Switch: Map an input to a specific switch.<br>- Switch Group: Map an input to a specific switch group. |
| Target | The target for this mapping. If Type is Default, leave blank. If Type is Switch, then enter the name of the switch (as defined in ShellyDevices: Devices: Outputs: Name). If Type is Switch Group, then enter the name of the switch group (as defined in ShellyDevices: Devices: Outputs: Group). |
| Input | The input (as defined in ShellyDevices: Devices: Inputs: Name) to map to the default, switch or switch group. |


### Section: Files

| Parameter | Description | 
|:--|:--|
| SavedStateFile | JSON file name to store the app's current state and history. | 
| Logfile | A text log file that records progress messages and warnings. | 
| LogfileMaxLines| Maximum number of lines to keep in the log file. If zero, file will never be truncated. | 
| LogfileVerbosity | The level of detail captured in the log file. One of: none; error; warning; summary; detailed; debug; all | 
| ConsoleVerbosity | Controls the amount of information written to the console. One of: error; warning; summary; detailed; debug; all. Errors are written to stderr all other messages are written to stdout | 
| MaxDaysSwitchChangeHistory | The state file keeps a log of the switch state changes. This option controls how many days of history we keep. Defaults to 30 days. | 

### Section: Email

| Parameter | Description | 
|:--|:--|
| EnableEmail | Set to *True* if you want to allow the app to send emails. If True, the remaining settings in this section must be configured correctly. | 
at the EnergyUsed entries for the last 7 days in the system_state.json file for your average usage. Set to blank or 0 to disable. | 
| SMTPServer | The SMTP host name that supports TLS encryption. If using a Google account, set to smtp.gmail.com |
| SMTPPort | The port number to use to connect to the SMTP server. If using a Google account, set to 587 |
| SMTPUsername | Your username used to login to the SMTP server. Alternatively, set the SMTP_USERNAME environment variable. If using a Google account, set to your Google email address. |
| SMTPPassword | The password used to login to the SMTP server. Alternatively, set the SMTP_PASSWORD environment variable. If using a Google account, create an app password for the PowerController at https://myaccount.google.com/apppasswords  |
| SubjectPrefix | Optional. If set, the app will add this text to the start of any email subject line for emails it sends. |

### Section: ViewerWebsite

Use this section to configure integration with the PowerControllerViewer app - see https://github.com/NickElseySpelloC/PowerControllerViewer

| Parameter | Description | 
|:--|:--|
| Enable | Set to True to enable integration with the PowerControllerViewer app | 
| BaseURL | The base URL of the PowerControllerViewer app | 
| AccessKey | The access key for the PowerControllerViewer app | 
| APITimeout | How long to wait in seconds for a response from the PowerControllerViewer app | 
| Frequency | How often to post the state to the web viewer app (in seconds) | 

### Section: HeartbeatMonitor

| Parameter | Description | 
|:--|:--|
| Enable | Set to True to enable integration with the Heartbeat monitoring service | 
| WebsiteURL | Each time the app runs successfully, you can have it hit this URL to record a heartbeat. This is optional. If the app exist with a fatal error, it will append /fail to this URL. | 
| HeartbeatTimeout | How long to wait for a response from the website before considering it down in seconds. | 
| Frequency | How often to post the state to the heartbeat monitor (in seconds) | 

# Setting up the Smart Switch

The Power Controller is currently designed to physically start or stop the pool device via Shelly and/or Tasmota smart switches. This is a relay that can be connected to your local Wi-Fi network and controlled remotely via an API call. A detailed setup guide is beyond the scope of this document, but the brief steps are as follows:
* Purchase a Smart Switch. 
  * For Shelly devices, see the [Models Library](https://nickelseyspelloc.github.io/sc-smart-device/shelly_models_list/) for a list of supported models and which of these have an energy meter built in.
  * For Tasmota devices, we have only tested with this device so far: `ESP32-C3 AU Plug V3`
* Install and configure the switch so that the relay output controls power to your device. Note the assigned IP address and model.
* If possible, create a DHCP reservation for the device in your local router so that the IP doesn't change.
* Update the SCSmartDevices section of your *config.yaml* file. See [Smart Devices library](https://nickelseyspelloc.github.io/sc-smart-device/) for details on how to configure this section.

# Logs and Data Files

The app will first look for these files in the current working directory and failing that, the same directory that the main.py file exists in. If the app needs to create any of these files, it will do so in the main.py folder.

* logfile.log: Progress messages and warnings are written to this file. The logging level is controlled by the LogfileVerbosity configuration parameter.
* system_state.json: Tracks past seven days of runtime and today's runtime. Please don't modify this file.

# Running the App

Setup the environment file:

```
ln -s .env.prod.template .env.target
```

Initially, you can run the app from the command line: 

```bash
launch.sh
```

## Loading changes

If you make changes to the config.yaml file, the app will automatically reload the file. Be sure to check the logfile afterwards to make sure there are no errors.


Check the logfile and make sure there are no errors. Test your lighting schedule and make sure your lights change as planned. 

# Running the App via systemd

This section shows you how to configure the app to run automatically at boot on a RaspberryPi.

## 1. Create a service file

Create a new service file and edit it to review:
```bash
sudo cp deploy/<env>/LightingControl.service /etc/systemd/system/LightingControl.service

sudo nano /etc/systemd/system/LightingControl.service
```


## 2. Enable and start the service

```bash
sudo systemctl daemon-reexec       # re-executes systemd in case of changes
sudo systemctl daemon-reload       # reload service files
sudo systemctl enable LightingControl   # enable on boot
sudo systemctl start LightingControl    # start now
```

## 3. View logs

```bash
journalctl -u LightingControl -f
```
