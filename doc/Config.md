

# CarConnectivity Connector for MySkoda Config Options
The configuration for CarConnectivity is a .json file.
## MySkoda Connector Options
These are the valid options for the MySkoda Connector
```json
{
    "carConnectivity": {
        "connectors": [
            {
                "type": "skoda", // Definition for the MySkoda Connector
                "config": {
                    "log_level": "error", // set the connectos log level
                    "interval": 300, // Interval in which the server is checked in seconds
                    "username": "test@test.de", // Username of your Volkswagen Account
                    "password": "testpassword123", // Username of your Volkswagen Account
                    "netrc": "~/.netr", // netrc file if to be used for passwords
                    "api_log_level": "debug", // Show debug information regarding the API
                    "max_age": 300 //Cache requests to the server vor MAX_AGE seconds
                }
            }
        ],
        "plugins": []
    }
}
```