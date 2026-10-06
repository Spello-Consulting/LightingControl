"""Configuration schemas for use with the SCConfigManager class."""


class ConfigSchema:
    """Base class for configuration schemas."""

    def __init__(self):

        self.placeholders = {
            "Email": {
                "SMTPUsername": "<Your SMTP username here>",
                "SMTPPassword": "<Your SMTP password here>",
            }
        }

        self.validation = {
            "General": {
                "type": "dict",
                "required": False,
                "nullable": True,
                "schema": {
                    "AppName": {"type": "string", "required": False, "nullable": True},
                    "CheckInterval": {"type": "number", "required": False, "nullable": True},
                    "PrintToConsole": {"type": "boolean", "required": False, "nullable": True},
                    "DisableAllSwitches": {"type": "boolean", "required": False, "nullable": True},
                },
            },
            "ViewerWebsite": {
                "type": "dict",
                "schema": {
                    "Enable": {"type": "boolean", "required": False, "nullable": True},
                    "Label": {"type": "string", "required": False, "nullable": True},
                    "BaseURL": {"type": "string", "required": False, "nullable": True},
                    "AccessKey": {"type": "string", "required": False, "nullable": True},
                    "APITimeout": {"type": "number", "required": False, "nullable": True, "min": 1, "max": 60},
                    "Frequency": {"type": "number", "required": False, "nullable": True, "min": 1, "max": 3600},
                },
            },
            "SCSmartDevices": {
                "schema": {
                    "ShellyWebhooks": {
                        "schema": {
                            "DrainFirst": {"type": "boolean", "required": False, "nullable": True},
                        },
                    },
                    "Devices": {
                        "schema": {
                            "schema": {
                                "Outputs": {
                                    "schema": {
                                        "schema": {
                                            "Group": {"type": "string", "required": False, "nullable": True},
                                        },
                                    },
                                },
                            },
                        },
                    },
                }
            },
            "Location": {
                "type": "dict",
                "required": False,
                "nullable": True,
                "schema": {
                    "GoogleMapsURL": {"type": "string", "required": False, "nullable": True},
                    "Timezone": {"type": "string", "required": False, "nullable": True},
                    "Latitude": {"type": "number", "required": False, "nullable": True},
                    "Longitude": {"type": "number", "required": False, "nullable": True},
                },
            },
            "Schedules": {
                "type": "list",
                "required": True,
                "nullable": False,
                "schema": {
                    "type": "dict",
                    "schema": {
                        "Name": {"type": "string", "required": True},
                        "Events": {
                            "type": "list",
                            "required": True,
                            "schema": {
                                "type": "dict",
                                "schema": {
                                    "TurnOn": {"type": "string", "required": True},
                                    "TurnOff": {"type": "string", "required": True},
                                    "RandomOffset": {"type": "number", "required": False, "nullable": True},
                                    "DaysOfWeek": {"type": "string", "required": False, "nullable": True},
                                    "DatesOff": {
                                        "type": "list",
                                        "required": False,
                                        "nullable": True,
                                        "schema": {
                                            "type": "dict",
                                            "schema": {
                                                "StartDate": {"type": "date", "required": False, "nullable": True},
                                                "EndDate": {"type": "date", "required": False, "nullable": True},
                                            },
                                        },
                                    },
                                },
                            },
                        },
                    },
                },
            },
            "LightingControl": {
                "type": "list",
                "required": True,
                "nullable": False,
                "schema": {
                    "type": "dict",
                    "schema": {
                        "Type": {"type": "string", "required": True, "allowed": ["Default", "Switch", "Switch Group"]},
                        "Target": {"type": "string", "required": False, "nullable": True},
                        "Schedule": {"type": "string", "required": True},
                    },
                },
            },
            "InputControls": {
                "type": "list",
                "required": False,
                "nullable": True,
                "schema": {
                    "type": "dict",
                    "schema": {
                        "Type": {"type": "string", "required": True, "allowed": ["Default", "Switch", "Switch Group"]},
                        "Target": {"type": "string", "required": False, "nullable": True},
                        "Input": {"type": "string", "required": True},
                    },
                },
            },
            "Files": {
                "type": "dict",
                "schema": {
                    "SavedStateFile": {"type": "string", "required": True},
                    "MaxDaysSwitchChangeHistory": {"type": "number", "required": False, "nullable": True, "min": 1, "max": 365},
                },
            },
            "Website": {
                "type": "dict",
                "required": False,
                "nullable": True,
                "schema": {
                    "Enable": {"type": "boolean", "required": False, "nullable": True},
                    "HostingIP": {"type": "string", "required": False, "nullable": True},
                    "Port": {"type": "number", "required": False, "nullable": True, "min": 1, "max": 65535},
                    "AccessKey": {"type": "string", "required": False, "nullable": True},
                    "DebugMode": {"type": "boolean", "required": False, "nullable": True},
                    "PageAutoRefresh": {"type": "number", "required": False, "nullable": True, "min": 0, "max": 3600},
                },
            },
        }
