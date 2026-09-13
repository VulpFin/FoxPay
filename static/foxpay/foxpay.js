(function (global) {
  function FoxPay(publishableKey, options) {
    var config = options || {};
    var baseUrl = config.baseUrl || "";
    return {
      checkout: function checkout(args) {
        if (!args || !args.clientSecret) {
          return Promise.reject(new Error("clientSecret is required"));
        }
        global.location.href = baseUrl + "/pay/" + encodeURIComponent(args.clientSecret) + "/";
        return Promise.resolve();
      },
      key: publishableKey
    };
  }

  global.FoxPay = FoxPay;
})(window);
