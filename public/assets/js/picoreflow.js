var state = "IDLE";
var state_last = "";
var graph = [ 'profile', 'live'];
var points = [];
var profiles = [];
var time_mode = 0;
var selected_profile = 0;
var selected_profile_name = 'cone-05-long-bisque.json';
var temp_scale = "c";
var time_scale_slope = "s";
var time_scale_profile = "h";
var time_scale_long = "Seconds";
var temp_scale_display = "C";
var kwh_rate = 0.26;
var currency_type = "EUR";

var protocol = 'ws:';
if (window.location.protocol == 'https:') {
    protocol = 'wss:';
}
var host = "" + protocol + "//" + window.location.hostname + ":" + window.location.port;
var ws_status = new WebSocket(host+"/status");
var ws_control = new WebSocket(host+"/control");
var ws_config = new WebSocket(host+"/config");
var ws_storage = new WebSocket(host+"/storage");


if(window.webkitRequestAnimationFrame) window.requestAnimationFrame = window.webkitRequestAnimationFrame;

graph.profile =
{
    label: "Profile",
    data: [],
    points: { show: false },
    color: "#75890c",
    draggable: false
};

graph.live =
{
    label: "Live",
    data: [],
    points: { show: false },
    color: "#d8d3c5",
    draggable: false
};


function updateProfile(id)
{
    selected_profile = id;
    selected_profile_name = profiles[id].name;
    var job_seconds = profiles[id].data.length === 0 ? 0 : parseInt(profiles[id].data[profiles[id].data.length-1][0]);
    var kwh = (3850*job_seconds/3600/1000).toFixed(2);
    var cost =  (kwh*kwh_rate).toFixed(2);
    var job_time = new Date(job_seconds * 1000).toISOString().substr(11, 8);
    $('#sel_prof').html(profiles[id].name);
    $('#sel_prof_eta').html(job_time);
    $('#sel_prof_cost').html(kwh + ' kWh ('+ currency_type +': '+ cost +')');
    graph.profile.data = profiles[id].data;
    graph.plot = $.plot("#graph_container", [ graph.profile, graph.live ] , getOptions());
}

function deleteProfile()
{
    var profile = { "type": "profile", "data": "", "name": selected_profile_name };
    var delete_struct = { "cmd": "DELETE", "profile": profile };

    var delete_cmd = JSON.stringify(delete_struct);
    console.log("Delete profile:" + selected_profile_name);

    ws_storage.send(delete_cmd);

    ws_storage.send('GET');
    selected_profile_name = profiles[0].name;

    state="IDLE";
    $('#edit').hide();
    $('#profile_selector').show();
    $('#btn_controls').show();
    $('#status').slideDown();
    $('#profile_table').slideUp();
    $('#e2').select2('val', 0);
    graph.profile.points.show = false;
    graph.profile.draggable = false;
    graph.plot = $.plot("#graph_container", [ graph.profile, graph.live ], getOptions());
}


function updateProgress(percentage)
{
    if(state=="RUNNING")
    {
        if(percentage > 100) percentage = 100;
        $('#progressBar').css('width', percentage+'%');
        if(percentage>5) $('#progressBar').html(parseInt(percentage)+'%');
    }
    else
    {
        $('#progressBar').css('width', 0+'%');
        $('#progressBar').html('');
    }
}

function updateProfileTable()
{
    var dps = 0;
    var slope = "";
    var color = "";

    var html = '<h3>Schedule Points</h3><div class="table-responsive" style="scroll: none"><table class="table table-striped">';
        html += '<tr><th style="width: 50px">#</th><th>Target Time in ' + time_scale_long+ '</th><th>Target Temperature in °'+temp_scale_display+'</th><th>Slope in &deg;'+temp_scale_display+'/'+time_scale_slope+'</th><th></th></tr>';

    for(var i=0; i<graph.profile.data.length;i++)
    {

        if (i>=1) dps =  ((graph.profile.data[i][1]-graph.profile.data[i-1][1])/(graph.profile.data[i][0]-graph.profile.data[i-1][0]) * 10) / 10;
        if (dps  > 0) { slope = "up";     color="rgba(206, 5, 5, 1)"; } else
        if (dps  < 0) { slope = "down";   color="rgba(23, 108, 204, 1)"; dps *= -1; } else
        if (dps == 0) { slope = "right";  color="grey"; }

        html += '<tr><td><h4>' + (i+1) + '</h4></td>';
        html += '<td><input type="text" class="form-control" id="profiletable-0-'+i+'" value="'+ timeProfileFormatter(graph.profile.data[i][0],true) + '" style="width: 60px" /></td>';
        html += '<td><input type="text" class="form-control" id="profiletable-1-'+i+'" value="'+ graph.profile.data[i][1] + '" style="width: 60px" /></td>';
        html += '<td><div class="input-group"><span class="glyphicon glyphicon-circle-arrow-' + slope + ' input-group-addon ds-trend" style="background: '+color+'"></span><input type="text" class="form-control ds-input" readonly value="' + formatDPS(dps) + '" style="width: 100px" /></div></td>';
        html += '<td>&nbsp;</td></tr>';
    }

    html += '</table></div>';

    $('#profile_table').html(html);

    //Link table to graph
    $(".form-control").change(function(e)
        {
            var id = $(this)[0].id; //e.currentTarget.attributes.id
            var value = parseInt($(this)[0].value);
            var fields = id.split("-");
            var col = parseInt(fields[1]);
            var row = parseInt(fields[2]);

            if (graph.profile.data.length > 0) {
            if (col == 0) {
                graph.profile.data[row][col] = timeProfileFormatter(value,false);
            }
            else {
                graph.profile.data[row][col] = value;
            }

            graph.plot = $.plot("#graph_container", [ graph.profile, graph.live ], getOptions());
            }
            updateProfileTable();

        });
}

function timeProfileFormatter(val, down) {
    var rval = val
    switch(time_scale_profile){
        case "m":
            if (down) {rval = val / 60;} else {rval = val * 60;}
            break;
        case "h":
            if (down) {rval = val / 3600;} else {rval = val * 3600;}
            break;
    }
    return Math.round(rval);
}

function formatDPS(val) {
    var tval = val;
    if (time_scale_slope == "m") {
        tval = val * 60;
    }
    if (time_scale_slope == "h") {
        tval = (val * 60) * 60;
    }
    return Math.round(tval);
}

function hazardTemp(){

    if (temp_scale == "f") {
        return (1500 * 9 / 5) + 32
    }
    else {
        return 1500
    }
}

function timeTickFormatter(val,axis)
{
// hours
if(axis.max>3600) {
  //var hours = Math.floor(val / (3600));
  //return hours;
  return Math.floor(val/3600);
  }

// minutes
if(axis.max<=3600) {
  return Math.floor(val/60);
  }

// seconds
if(axis.max<=60) {
  return val;
  }
}

function runTask()
{
    var cmd =
    {
        "cmd": "RUN",
        "profile": profiles[selected_profile]
    }

    graph.live.data = [];
    graph.plot = $.plot("#graph_container", [ graph.profile, graph.live ] , getOptions());

    ws_control.send(JSON.stringify(cmd));

}

function runTaskSimulation()
{
    var cmd =
    {
        "cmd": "SIMULATE",
        "profile": profiles[selected_profile]
    }

    graph.live.data = [];
    graph.plot = $.plot("#graph_container", [ graph.profile, graph.live ] , getOptions());

    ws_control.send(JSON.stringify(cmd));

}


function abortTask()
{
    var cmd = {"cmd": "STOP"};
    ws_control.send(JSON.stringify(cmd));
}

function enterNewMode()
{
    state="EDIT"
    $('#status').slideUp();
    $('#edit').show();
    $('#profile_selector').hide();
    $('#btn_controls').hide();
    $('#form_profile_name').attr('value', '');
    $('#form_profile_name').attr('placeholder', 'Please enter a name');
    graph.profile.points.show = true;
    graph.profile.draggable = true;
    graph.profile.data = [];
    graph.plot = $.plot("#graph_container", [ graph.profile, graph.live ], getOptions());
    updateProfileTable();
}

function enterEditMode()
{
    state="EDIT"
    $('#status').slideUp();
    $('#edit').show();
    $('#profile_selector').hide();
    $('#btn_controls').hide();
    console.log(profiles);
    $('#form_profile_name').val(profiles[selected_profile].name);
    graph.profile.points.show = true;
    graph.profile.draggable = true;
    graph.plot = $.plot("#graph_container", [ graph.profile, graph.live ], getOptions());
    updateProfileTable();
}

function leaveEditMode()
{
    selected_profile_name = $('#form_profile_name').val();
    ws_storage.send('GET');
    state="IDLE";
    $('#edit').hide();
    $('#profile_selector').show();
    $('#btn_controls').show();
    $('#status').slideDown();
    $('#profile_table').slideUp();
    graph.profile.points.show = false;
    graph.profile.draggable = false;
    graph.plot = $.plot("#graph_container", [ graph.profile, graph.live ], getOptions());
}

function newPoint()
{
    if(graph.profile.data.length > 0)
    {
        var pointx = parseInt(graph.profile.data[graph.profile.data.length-1][0])+15;
    }
    else
    {
        var pointx = 0;
    }
    graph.profile.data.push([pointx, Math.floor((Math.random()*230)+25)]);
    graph.plot = $.plot("#graph_container", [ graph.profile, graph.live ], getOptions());
    updateProfileTable();
}

function delPoint()
{
    graph.profile.data.splice(-1,1)
    graph.plot = $.plot("#graph_container", [ graph.profile, graph.live ], getOptions());
    updateProfileTable();
}

function toggleTable()
{
    if($('#profile_table').css('display') == 'none')
    {
        $('#profile_table').slideDown();
    }
    else
    {
        $('#profile_table').slideUp();
    }
}

function saveProfile()
{
    name = $('#form_profile_name').val();
    var rawdata = graph.plot.getData()[0].data
    var data = [];
    var last = -1;

    for(var i=0; i<rawdata.length;i++)
    {
        if(rawdata[i][0] > last)
        {
          data.push([rawdata[i][0], rawdata[i][1]]);
        }
        else
        {
          $.bootstrapGrowl("<span class=\"glyphicon glyphicon-exclamation-sign\"></span> <b>ERROR 88:</b><br/>An oven is not a time-machine", {
            ele: 'body', // which element to append to
            type: 'alert', // (null, 'info', 'error', 'success')
            offset: {from: 'top', amount: 250}, // 'top', or 'bottom'
            align: 'center', // ('left', 'right', or 'center')
            width: 385, // (integer, or 'auto')
            delay: 5000,
            allow_dismiss: true,
            stackup_spacing: 10 // spacing between consecutively stacked growls.
          });

          return false;
        }

        last = rawdata[i][0];
    }

    var profile = { "type": "profile", "data": data, "name": name }
    var put = { "cmd": "PUT", "profile": profile }

    var put_cmd = JSON.stringify(put);

    ws_storage.send(put_cmd);

    leaveEditMode();
}

function get_tick_size() {
//switch(time_scale_profile){
//  case "s":
//    return 1;
//  case "m":
//    return 60;
//  case "h":
//    return 3600;
//  }
return 3600;
}

function getOptions()
{

  var options =
  {

    series:
    {
        lines:
        {
            show: true
        },

        points:
        {
            show: true,
            radius: 5,
            symbol: "circle"
        },

        shadowSize: 3

    },

	xaxis:
    {
      min: 0,
      tickColor: 'rgba(216, 211, 197, 0.2)',
      tickFormatter: timeTickFormatter,
      tickSize: get_tick_size(),
      font:
      {
        size: 14,
        lineHeight: 14,        weight: "normal",
        family: "Digi",
        variant: "small-caps",
        color: "rgba(216, 211, 197, 0.85)"
      }
	},

	yaxis:
    {
      min: 0,
      tickDecimals: 0,
      draggable: false,
      tickColor: 'rgba(216, 211, 197, 0.2)',
      font:
      {
        size: 14,
        lineHeight: 14,
        weight: "normal",
        family: "Digi",
        variant: "small-caps",
        color: "rgba(216, 211, 197, 0.85)"
      }
	},

	grid:
    {
	  color: 'rgba(216, 211, 197, 0.55)',
      borderWidth: 1,
      labelMargin: 10,
      mouseActiveRadius: 50
	},

    legend:
    {
      show: false
    }
  }

  return options;

}



$(document).ready(function()
{

    if(!("WebSocket" in window))
    {
        $('#chatLog, input, button, #examples').fadeOut("fast");
        $('<p>Oh no, you need a browser that supports WebSockets. How about <a href="http://www.google.com/chrome">Google Chrome</a>?</p>').appendTo('#container');
    }
    else
    {

        // Status Socket ////////////////////////////////

        ws_status.onopen = function()
        {
            console.log("Status Socket has been opened");

//            $.bootstrapGrowl("<span class=\"glyphicon glyphicon-exclamation-sign\"></span>Getting data from server",
//            {
//            ele: 'body', // which element to append to
//            type: 'success', // (null, 'info', 'error', 'success')
//            offset: {from: 'top', amount: 250}, // 'top', or 'bottom'
//            align: 'center', // ('left', 'right', or 'center')
//            width: 385, // (integer, or 'auto')
//            delay: 2500,
//            allow_dismiss: true,
//            stackup_spacing: 10 // spacing between consecutively stacked growls.
//            });
        };

        ws_status.onclose = function()
        {
            $.bootstrapGrowl("<span class=\"glyphicon glyphicon-exclamation-sign\"></span> <b>ERROR 1:</b><br/>Status Websocket not available", {
            ele: 'body', // which element to append to
            type: 'error', // (null, 'info', 'error', 'success')
            offset: {from: 'top', amount: 250}, // 'top', or 'bottom'
            align: 'center', // ('left', 'right', or 'center')
            width: 385, // (integer, or 'auto')
            delay: 5000,
            allow_dismiss: true,
            stackup_spacing: 10 // spacing between consecutively stacked growls.
          });
        };

        ws_status.onmessage = function(e)
        {
            console.log("received status data")
            console.log(e.data);

            x = JSON.parse(e.data);
            if (x.type == "backlog")
            {
                if (x.profile)
                {
                    selected_profile_name = x.profile.name;
                    $.each(profiles,  function(i,v) {
                        if(v.name == x.profile.name) {
                            updateProfile(i);
                            $('#e2').select2('val', i);
                        }
                    });
                }

                $.each(x.log, function(i,v) {
                    graph.live.data.push([v.runtime, v.temperature]);
                    graph.plot = $.plot("#graph_container", [ graph.profile, graph.live ] , getOptions());
                });
            }

            if(state!="EDIT")
            {
                state = x.state;

                if (state!=state_last)
                {
                    if(state_last == "RUNNING")
                    {
                        $('#target_temp').html('---');
                        updateProgress(0);
                        $.bootstrapGrowl("<span class=\"glyphicon glyphicon-exclamation-sign\"></span> <b>Run completed</b>", {
                        ele: 'body', // which element to append to
                        type: 'success', // (null, 'info', 'error', 'success')
                        offset: {from: 'top', amount: 250}, // 'top', or 'bottom'
                        align: 'center', // ('left', 'right', or 'center')
                        width: 385, // (integer, or 'auto')
                        delay: 0,
                        allow_dismiss: true,
                        stackup_spacing: 10 // spacing between consecutively stacked growls.
                        });
                    }
                }

                if(state=="RUNNING")
                {
                    $("#nav_start").hide();
                    $("#nav_stop").show();

                    graph.live.data.push([x.runtime, x.temperature]);
                    graph.plot = $.plot("#graph_container", [ graph.profile, graph.live ] , getOptions());

                    left = parseInt(x.totaltime-x.runtime);
                    eta = new Date(left * 1000).toISOString().substr(11, 8);

                    updateProgress(parseFloat(x.runtime)/parseFloat(x.totaltime)*100);
                    $('#state').html('<span class="glyphicon glyphicon-time" style="font-size: 22px; font-weight: normal"></span><span style="font-family: Digi; font-size: 40px;">' + eta + '</span>');
                    $('#target_temp').html(parseInt(x.target));
                    $('#cost').html(x.currency_type + parseFloat(x.cost).toFixed(2));
                  


                }
                else
                {
                    $("#nav_start").show();
                    $("#nav_stop").hide();
                    $('#state').html('<p class="ds-text">'+state+'</p>');
                }

                $('#act_temp').html(parseInt(x.temperature));
                heat_rate = parseInt(x.heat_rate)
                if (heat_rate > 9999) { heat_rate = 9999; }
                if (heat_rate < -9999) { heat_rate = -9999; }
                $('#heat_rate').html(heat_rate);
                $('#heat').html('<div class="bar" style="height:'+x.pidstats.out*70+'%;"></div>')
                if (x.cool > 0.5) { $('#cool').addClass("ds-led-cool-active"); } else { $('#cool').removeClass("ds-led-cool-active"); }
                if (x.air > 0.5) { $('#air').addClass("ds-led-air-active"); } else { $('#air').removeClass("ds-led-air-active"); }
                if (x.temperature > hazardTemp()) { $('#hazard').addClass("ds-led-hazard-active"); } else { $('#hazard').removeClass("ds-led-hazard-active"); }
                if ((x.door == "OPEN") || (x.door == "UNKNOWN")) { $('#door').addClass("ds-led-door-open"); } else { $('#door').removeClass("ds-led-door-open"); }

                state_last = state;

            }
        };

        // Config Socket /////////////////////////////////

        ws_config.onopen = function()
        {
            ws_config.send('GET');
        };

        ws_config.onmessage = function(e)
        {
            console.log (e.data);
            x = JSON.parse(e.data);
            temp_scale = x.temp_scale;
            time_scale_slope = x.time_scale_slope;
            time_scale_profile = x.time_scale_profile;
            kwh_rate = x.kwh_rate;
            currency_type = x.currency_type;

            if (temp_scale == "c") {temp_scale_display = "C";} else {temp_scale_display = "F";}


            $('#act_temp_scale').html('º'+temp_scale_display);
            $('#target_temp_scale').html('º'+temp_scale_display);
            $('#heat_rate_temp_scale').html('º'+temp_scale_display);

            switch(time_scale_profile){
                case "s":
                    time_scale_long = "Seconds";
                    break;
                case "m":
                    time_scale_long = "Minutes";
                    break;
                case "h":
                    time_scale_long = "Hours";
                    break;
            }

        }

        // Control Socket ////////////////////////////////

        ws_control.onopen = function()
        {

        };

        ws_control.onmessage = function(e)
        {
            //Data from Simulation
            console.log ("control socket has been opened")
            console.log (e.data);
            x = JSON.parse(e.data);
            graph.live.data.push([x.runtime, x.temperature]);
            graph.plot = $.plot("#graph_container", [ graph.profile, graph.live ] , getOptions());

        }

        // Storage Socket ///////////////////////////////

        ws_storage.onopen = function()
        {
            ws_storage.send('GET');
        };


        ws_storage.onmessage = function(e)
        {
            message = JSON.parse(e.data);

            if(message.resp)
            {
                if(message.resp == "FAIL")
                {
                    if (confirm('Overwrite?'))
                    {
                        message.force=true;
                        console.log("Sending: " + JSON.stringify(message));
                        ws_storage.send(JSON.stringify(message));
                    }
                    else
                    {
                        //do nothing
                    }
                }

                return;
            }

            //the message is an array of profiles
            //FIXME: this should be better, maybe a {"profiles": ...} container?
            profiles = message;
            //delete old options in select
            $('#e2').find('option').remove().end();
            // check if current selected value is a valid profile name
            // if not, update with first available profile name
            var valid_profile_names = profiles.map(function(a) {return a.name;});
            if (
              valid_profile_names.length > 0 &&
              $.inArray(selected_profile_name, valid_profile_names) === -1
            ) {
              selected_profile = 0;
              selected_profile_name = valid_profile_names[0];
            }

            // fill select with new options from websocket
            for (var i=0; i<profiles.length; i++)
            {
                var profile = profiles[i];
                //console.log(profile.name);
                $('#e2').append('<option value="'+i+'">'+profile.name+'</option>');

                if (profile.name == selected_profile_name)
                {
                    selected_profile = i;
                    $('#e2').select2('val', i);
                    updateProfile(i);
                }
            }
        };


        $("#e2").select2(
        {
            placeholder: "Select Profile",
            allowClear: true,
            minimumResultsForSearch: -1
        });


        $("#e2").on("change", function(e)
        {
            updateProfile(e.val);
        });

    }
});

// ===================================================================
// Settings (config UI) - GET/POST /api/config
// ===================================================================

function loadSettings() {
    $.ajax({
        url: "/api/config",
        type: "GET",
        success: function(data) {
            renderSettings(data);
        },
        error: function() {
            $("#settingsBody").html("<p class='text-danger'>Failed to load settings (auth required?).</p>");
        }
    });
}

function renderSettings(cfg) {
    var groups = {
        "PID tuning": [
            "pid_kp", "pid_ki", "pid_kd", "pid_control_window",
            "pid_d_spike_limit_enabled", "pid_d_spike_limit", "pid_d_filter_alpha",
            "throttle_below_temp", "throttle_percent"
        ],
        "Cost / display": [
            "kwh_rate", "kw_elements", "currency_type",
            "temp_scale", "time_scale_slope", "time_scale_profile"
        ],
        "Safety": [
            "emergency_shutoff_temp", "kiln_must_catch_up",
            "hold_auto_extend", "hold_at_temp_tolerance",
            "thermocouple_offset",
            "element_failure_detection",
            "element_failure_min_full_duty_seconds",
            "element_failure_min_heat_rate", "element_failure_min_temp",
            "cool_down_safe_open_temp",
            "cool_down_notify_on_complete", "cool_down_notify_on_safe_open",
            "multi_tc_delta_alert_degrees"
        ],
        "Notifications": [
            "notify_email_enabled", "notify_email_to",
            "notify_pushover_enabled",
            "notify_ntfy_enabled", "notify_ntfy_topic",
            "notify_slack_enabled"
        ]
    };

    var html = "";
    for (var group in groups) {
        html += "<h4>" + group + "</h4><table class='table table-condensed'><tbody>";
        groups[group].forEach(function(key) {
            if (!(key in cfg)) return;
            var v = cfg[key];
            var input;
            if (typeof v === "boolean") {
                input = "<input type='checkbox' data-key='" + key + "' data-type='bool'" +
                        (v ? " checked" : "") + ">";
            } else if (typeof v === "number") {
                input = "<input type='number' step='any' class='form-control' " +
                        "data-key='" + key + "' data-type='number' value='" + v + "'>";
            } else if (Array.isArray(v)) {
                input = "<input type='text' class='form-control' " +
                        "data-key='" + key + "' data-type='list' value='" +
                        v.join(", ") + "'>";
            } else {
                input = "<input type='text' class='form-control' " +
                        "data-key='" + key + "' data-type='string' value='" +
                        (v == null ? "" : v) + "'>";
            }
            html += "<tr><td style='width:55%'><code>" + key + "</code></td>" +
                    "<td>" + input + "</td></tr>";
        });
        html += "</tbody></table>";
    }
    $("#settingsBody").html(html);
}

function saveSettings() {
    var payload = {};
    $("#settingsBody [data-key]").each(function() {
        var $el = $(this);
        var key = $el.data("key");
        var t = $el.data("type");
        var v;
        if (t === "bool") {
            v = $el.is(":checked");
        } else if (t === "number") {
            v = parseFloat($el.val());
            if (isNaN(v)) return;
        } else if (t === "list") {
            v = $el.val().split(",").map(function(s) { return s.trim(); })
                                  .filter(function(s) { return s.length > 0; });
        } else {
            v = $el.val();
        }
        payload[key] = v;
    });
    $.ajax({
        url: "/api/config",
        type: "POST",
        contentType: "application/json",
        data: JSON.stringify(payload),
        success: function(resp) {
            if (resp && resp.success) {
                $.bootstrapGrowl("Settings saved.", { type: "success", delay: 2000 });
                $("#settingsModal").modal("hide");
            } else {
                $.bootstrapGrowl("Save failed.", { type: "error", delay: 4000 });
            }
        },
        error: function() {
            $.bootstrapGrowl("Save failed (auth required?).", { type: "error", delay: 4000 });
        }
    });
}

// ===================================================================
// Firing history (#8) - GET /api/history, GET /api/history/<id>
// ===================================================================

function loadHistory() {
    $.ajax({
        url: "/api/history?limit=50",
        type: "GET",
        success: function(data) {
            renderHistoryList(data && data.firings ? data.firings : []);
        },
        error: function() {
            $("#historyBody").html("<p class='text-danger'>Failed to load history (auth required, or history disabled).</p>");
        }
    });
}

function renderHistoryList(firings) {
    if (!firings.length) {
        $("#historyBody").html("<p>No firings recorded yet.</p>");
        return;
    }
    var html = "<table class='table table-striped'>";
    html += "<thead><tr><th>#</th><th>Started</th><th>Profile</th><th>Outcome</th>" +
            "<th>Peak</th><th>Cost</th><th></th></tr></thead><tbody>";
    firings.forEach(function(f) {
        html += "<tr>";
        html += "<td>" + f.id + "</td>";
        html += "<td>" + (f.started_at || "") + "</td>";
        html += "<td>" + (f.profile_name || "") + "</td>";
        html += "<td>" + (f.outcome || "") + "</td>";
        html += "<td>" + (f.peak_temp != null ? Math.round(f.peak_temp) : "") + "</td>";
        html += "<td>" + (f.currency_type || "") +
                (f.total_cost != null ? f.total_cost.toFixed(2) : "") + "</td>";
        html += "<td><button class='btn btn-xs btn-default' onclick='showFiring(" + f.id + ")'>View</button> " +
                "<button class='btn btn-xs btn-danger' onclick='deleteFiring(" + f.id + ")'>Delete</button></td>";
        html += "</tr>";
    });
    html += "</tbody></table><div id='firingDetail'></div>";
    $("#historyBody").html(html);
}

function showFiring(id) {
    $.ajax({
        url: "/api/history/" + id,
        type: "GET",
        success: function(f) {
            if (!f) {
                $("#firingDetail").html("<p>Not found.</p>");
                return;
            }
            var samples = f.samples || [];
            var rows = samples.map(function(s) {
                return "<tr><td>" + Math.round(s.runtime_s) + "</td>" +
                       "<td>" + (s.target_temp != null ? Math.round(s.target_temp) : "") + "</td>" +
                       "<td>" + (s.actual_temp != null ? Math.round(s.actual_temp) : "") + "</td>" +
                       "<td>" + (s.heat_rate != null ? Math.round(s.heat_rate) : "") + "</td></tr>";
            }).join("");
            // tiny inline table; the full graph could be added here later
            var html = "<h4>Firing #" + f.id + " - " + f.profile_name + "</h4>";
            html += "<p>Started: " + f.started_at + "<br/>Ended: " +
                    (f.ended_at || "(in progress)") + "<br/>Outcome: " + f.outcome +
                    "<br/>Peak: " + Math.round(f.peak_temp || 0) +
                    "<br/>Cost: " + (f.currency_type || "") +
                    (f.total_cost != null ? f.total_cost.toFixed(2) : "") +
                    "<br/>Notes: " + (f.notes || "") + "</p>";
            html += "<div style='max-height:300px;overflow-y:auto'><table class='table table-condensed'>";
            html += "<thead><tr><th>t (s)</th><th>target</th><th>actual</th><th>deg/hr</th></tr></thead>";
            html += "<tbody>" + rows + "</tbody></table></div>";
            $("#firingDetail").html(html);
        }
    });
}

function deleteFiring(id) {
    if (!confirm("Delete firing #" + id + "?")) return;
    $.ajax({
        url: "/api/history/" + id,
        type: "DELETE",
        success: function() { loadHistory(); }
    });
}

// ===================================================================
// Orton ramp/hold importer (#10) - POST /api/profile/import
// ===================================================================

function submitOrtonImport() {
    var raw = $("#ortonImportText").val();
    var spec;
    try {
        spec = JSON.parse(raw);
    } catch (e) {
        $("#ortonImportResult").html("<span class='text-danger'>Invalid JSON: " + e + "</span>");
        return;
    }
    spec.save = true;
    $.ajax({
        url: "/api/profile/import",
        type: "POST",
        contentType: "application/json",
        data: JSON.stringify(spec),
        success: function(resp) {
            if (resp.success) {
                $("#ortonImportResult").html(
                    "<span class='text-success'>Imported as <b>" + resp.profile.name + "</b> (" +
                    resp.profile.data.length + " waypoints). Reloading profile list...</span>");
                ws_storage.send("GET");
                setTimeout(function() { $("#ortonImportModal").modal("hide"); }, 1500);
            } else {
                $("#ortonImportResult").html(
                    "<span class='text-danger'>Error: " + (resp.error || "unknown") + "</span>");
            }
        },
        error: function(xhr) {
            var msg = "request failed";
            try { msg = JSON.parse(xhr.responseText).error || msg; } catch (e) {}
            $("#ortonImportResult").html("<span class='text-danger'>" + msg + "</span>");
        }
    });
}
