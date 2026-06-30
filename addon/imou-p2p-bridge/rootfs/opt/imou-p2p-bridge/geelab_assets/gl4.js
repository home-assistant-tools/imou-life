"v4.2.0 Geelab Inc.";

(function (window) {
    "use strict";
    if (typeof window === 'undefined') {
        throw new Error('Geelab requires browser environment');
    }

var document = window.document;
var Math = window.Math;
var head = document.getElementsByTagName("head")[0];
var TIMEOUT = 10000;

function _Object(obj) {
    this._obj = obj;
}

_Object.prototype = {
    _each: function (process) {
        var _obj = this._obj;
        for (var k in _obj) {
            if (_obj.hasOwnProperty(k)) {
                process(k, _obj[k]);
            }
        }
        return this;
    },
    _extend: function (obj){
        var self = this;
        new _Object(obj)._each(function (key, value){
            self._obj[key] = value;
        })
    }
};

var uuid = function () {
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function (c) {
            var r = Math.random() * 16 | 0;
            var v = c === 'x' ? r : (r & 0x3 | 0x8);
            return v.toString(16);
        });
    };

function Config(config) {
    var self = this;
    new _Object(config)._each(function (key, value) {
        self[key] = value;
    });
}

Config.prototype = {
    apiServers: ['cap-global.geelabapi.com','cap-eu.geelabapi.com','cap-na.geelabapi.com'],
    staticServers: ["static.geelabapi.com"],
    protocol: 'http://',
    typePath: '/load',
    fallback_config: {
        bypass: {
            staticServers: ["static.geelabapi.com"],
            type: 'bypass',
            bypass: '/v4/geebypass.js'
        }
    },
    _get_fallback_config: function () {
        var self = this;
        if (isString(self.type)) {
            return self.fallback_config[self.type];
        } else {
            return self.fallback_config.bypass;
        }
    },
    _extend: function (obj) {
        var self = this;
        new _Object(obj)._each(function (key, value) {
            self[key] = value;
        })
    }
};
var isNumber = function (value) {
    return (typeof value === 'number');
};
var isString = function (value) {
    return (typeof value === 'string');
};
var isBoolean = function (value) {
    return (typeof value === 'boolean');
};
var isObject = function (value) {
    return (typeof value === 'object' && value !== null);
};
var isFunction = function (value) {
    return (typeof value === 'function');
};
var MOBILE = /Mobi/i.test(navigator.userAgent);

var callbacks = {};
var status = {};

var random = function () {
    return parseInt(Math.random() * 10000) + (new Date()).valueOf();
};

// bind function polyfill, bind without new functionality

var bind = function(target,context){
    if(typeof target !== 'function'){
        return;
    }
    var args = Array.prototype.slice.call(arguments,2);

    if(Function.prototype.bind){
        return target.bind(context, args);
    }else {
        return function(){
            var _args = Array.prototype.slice.call(arguments);
            return target.apply(context,args.concat(_args));
        }
    }
}



var toString = Object.prototype.toString;

var _isFunction = function(obj) {
  return typeof(obj) === 'function';
};
var _isObject = function(obj) {
  return obj === Object(obj);
};
var _isArray = function(obj) {
  return toString.call(obj) == '[object Array]';
};
var _isDate = function(obj) {
  return toString.call(obj) == '[object Date]';
};
var _isRegExp = function(obj) {
  return toString.call(obj) == '[object RegExp]';
};
var _isBoolean = function(obj) {
  return toString.call(obj) == '[object Boolean]';
};


function resolveKey(input){
  return  input.replace(/(\S)(_([a-zA-Z]))/g, function(match, $1, $2, $3){
          return $1 + $3.toUpperCase() || "";
  })
}

function camelizeKeys(input, convert){
  if(!_isObject(input) || _isDate(input) || _isRegExp(input) || _isBoolean(input) || _isFunction(input)){
      return convert ? resolveKey(input) : input;
  }

  if(_isArray(input)){
      var temp = [];
      for(var i = 0; i < input.length; i++){
          temp.push(camelizeKeys(input[i]));
      }

  }else {
      var temp = {};
      for(var prop in input){
          if(input.hasOwnProperty(prop)){
              temp[camelizeKeys(prop, true)] = camelizeKeys(input[prop]);
          }
      }
  }
  return temp;
}

var loadScript = function (url, cb, timeout) {
    var script = document.createElement("script");
    script.charset = "UTF-8";
    script.async = true;

    // Add crossOrigin for geelab static resources
    if ( /static\.geelab\.tech/g.test(url)) {
        script.crossOrigin = "anonymous";
    }

    script.onerror = function () {
        cb(true);
        // Error triggered, timeout logic is no longer needed
        loaded = true;
    };
    var loaded = false;
    script.onload = script.onreadystatechange = function () {
        if (!loaded &&
            (!script.readyState ||
            "loaded" === script.readyState ||
            "complete" === script.readyState)) {

            loaded = true;
            setTimeout(function () {
                cb(false);
            }, 0);
        }
    };
    script.src = url;
    head.appendChild(script);

    setTimeout(function () {
        if (!loaded) {
            script.onerror = script.onload = null;
            script.remove && script.remove();
            cb(true);
        }
    }, timeout || TIMEOUT);
};

var normalizeDomain = function (domain) {
    // special domain: uems.sysu.edu.cn/jwxt/geelab/
    // return domain.replace(/^https?:\/\/|\/.*$/g, ''); uems.sysu.edu.cn
    return domain.replace(/^https?:\/\/|\/$/g, ''); // uems.sysu.edu.cn/jwxt/geelab
};
var normalizePath = function (path) {

    path = path && path.replace(/\/+/g, '/');
    if (path.indexOf('/') !== 0) {
        path = '/' + path;
    }
    return path;
};
var normalizeQuery = function (query) {
    if (!query) {
        return '';
    }
    var q = '?';
    new _Object(query)._each(function (key, value) {
        if (isString(value) || isNumber(value) || isBoolean(value)) {
            q = q + encodeURIComponent(key) + '=' + encodeURIComponent(value) + '&';
        }
    });
    if (q === '?') {
        q = '';
    }
    return q.replace(/&$/, '');
};
var makeURL = function (protocol, domain, path, query) {
    domain = normalizeDomain(domain);

    var url = normalizePath(path) + normalizeQuery(query);
    if (domain) {
        url = protocol + domain + url;
    }

    return url;
};

var load = function (config, protocol, domains, path, query, cb, handleCb) {
    var tryRequest = function (at) {
        // Handle jsonp callback, ensure each different jsonp has a unique callback function
        if(handleCb){
            var cbName = "geetest_" + random();
            // Need to match predefined cbname parameter, delete object
            window[cbName] = bind(handleCb, null, cbName);
            query.callback = cbName;
        }
        var url = makeURL(protocol, domains[at], path, query);
        loadScript(url, function (err) {
            if (err) {
                // Remove callback when timeout or error occurs
                if(cbName){
                  try {
                     window[cbName] = function(){
                       window[cbName] = null;
                      }
                    } catch (e) {}
                }

                if (at >= domains.length - 1) {
                    cb(true);
                    // report gettype error
                } else {
                    tryRequest(at + 1);
                }
            } else {
                cb(false);
            }
        }, config.timeout);
    };
    tryRequest(0);
};


var jsonp = function (domains, path, config, callback) {

    var handleCb = function (cbName, data) {

        // Ensure it executes only once, won't trigger again in case of all timeouts

        if (data.status == 'success') {
            callback(data.data);
        } else if (!data.status) {
            callback(data);
        } else {
            // Interface has response, but returned error status, enter error handling logic
            callback(data);
        }
        window[cbName] = undefined;
        try {
            delete window[cbName];
        } catch (e) {
        }
    };
    load(config, config.protocol, domains, path, {
        callback: '',
        captcha_id: config.captchaId,
        challenge: config.challenge || uuid(),
        client_type: config.clientType ? config.clientType : (MOBILE? 'h5':'web'),
        risk_type: config.riskType,
        user_info: config.userInfo,
        call_type: config.callType,
        lang: config.language? config.language : navigator.appName === 'Netscape' ? navigator.language.toLowerCase() : navigator.userLanguage.toLowerCase()
    }, function (err) {
        // Network issue, interface has no response, directly use local verification code, enter offline mode
        // User logic can be added here
            if(err && typeof config.offlineCb === 'function'){
                // Execute own offline mode
                config.offlineCb();
                return;
            }
           if(err){
            callback(config._get_fallback_config());
           }
    }, handleCb);
};

var throwError = function (errorType, config, errObj) {
    var errors = {
        networkError: 'Network error',
        gtTypeError: 'gt field is not a string type'
    };
    if (typeof config.onError === 'function') {
        config.onError({
            desc: errObj.desc,
            msg: errObj.msg,
            code: errObj.code
        });
    } else {
        throw new Error(errors[errorType]);
    }
};

var detect = function () {
    return window.Geelab || document.getElementById("gt_lib");
};

if (detect()) {
    status.slide = "loaded";
}
var GeelabIsLoad = function (fname) {
  var GeelabIsLoad = false;
  var tags = { js: 'script', css: 'link' };
  var tagname = fname && tags[fname.split('.').pop()];
  if (tagname !== undefined) {
    var elts = document.getElementsByTagName(tagname);
    for (var i in elts) {
      if ((elts[i].href && elts[i].href.toString().indexOf(fname) > 0)
              || (elts[i].src && elts[i].src.toString().indexOf(fname) > 0)) {
        GeelabIsLoad = true;
      }
    }
  }
  return GeelabIsLoad;
};
window.initGeetest4 = function (userConfig,callback) {

    var config = new Config(userConfig);
    if (userConfig.https) {
        config.protocol = 'https://';
    } else if (!userConfig.protocol) {
        config.protocol = window.location.protocol + '//';
    }


    if (isObject(userConfig.getType)) {
        config._extend(userConfig.getType);
    }

    jsonp(config.apiServers , config.typePath, config, function (newConfig) {
            // Error catching, the first load request may directly report an error
            var newConfig = camelizeKeys(newConfig);

            if(newConfig.status === 'error'){
               return throwError('networkError', config, newConfig);
            }

            var type = newConfig.type;
            if(config.debug){
                new _Object(newConfig)._extend(config.debug)
            }
            var init = function () {
                config._extend(newConfig);
                callback(new window.Geetest4(config));
            };

            callbacks[type] = callbacks[type] || [];

            var s = status[type] || 'init';
            if (s === 'init') {
                status[type] = 'loading';

                callbacks[type].push(init);
                
                if(newConfig.gctPath){
                    load(config, config.protocol, Object.hasOwnProperty.call(config, 'staticServers') ? config.staticServers  : newConfig.staticServers || config.staticServers , newConfig.gctPath, null, function (err){
                        if(err){
                            throwError('networkError', config, {
                                code: '60205',
                                msg: 'Network failure',
                                desc: {
                                    detail: 'gct resource load timeout'
                                }
                            });
                        }
                    })
                }

                load(config,  config.protocol, Object.hasOwnProperty.call(config, 'staticServers') ? config.staticServers  : newConfig.staticServers || config.staticServers, newConfig.bypass || (newConfig.staticPath + newConfig.js), null, function (err) {
                    if (err) {
                        status[type] = 'fail';
                        throwError('networkError', config, {
                            code: '60204',
                            msg: 'Network failure',
                            desc: {
                                detail: 'js resource load timeout'
                            }
                        });
                    } else {

                        status[type] = 'loaded';
                        var cbs = callbacks[type];
                        for (var i = 0, len = cbs.length; i < len; i = i + 1) {
                            var cb = cbs[i];
                            if (isFunction(cb)) {
                                cb();
                            }
                        }
                        callbacks[type] = [];
                        status[type] = 'init';
                    }
                });
            } else if (s === "loaded") {
                // Check if gct needs to be reloaded
                if(newConfig.gctPath && !GeelabIsLoad(newConfig.gctPath)){
                  load(config, config.protocol, Object.hasOwnProperty.call(config, 'staticServers') ? config.staticServers  : newConfig.staticServers || config.staticServers , newConfig.gctPath, null, function (err){
                      if(err){
                          throwError('networkError', config, {
                              code: '60205',
                              msg: 'Network failure',
                              desc: {
                                  detail: 'gct resource load timeout'
                              }
                          });
                      }
                  })
                }
                return  init();
            } else if (s === "fail") {
              throwError('networkError', config, {
                code: '60204',
                msg: 'Network failure',
                desc: {
                    detail: 'js resource load timeout'
                }
              });
            } else if (s === "loading") {
                callbacks[type].push(init);
            }
        });

};


})(window);
