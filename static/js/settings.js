// ══════════════════════════════════════════════════════
//  settings.js — Multi-provider settings with nested models
//  Brand SVG paths from LobeHub Icons (MIT License)
//  https://github.com/lobehub/lobe-icons
// ══════════════════════════════════════════════════════

/** Cached server config loaded on first openSettings() */
var _serverConfig = null;

// ══════════════════════════════════════════════════════
//  Brand Icons — SVG paths from lobehub/lobe-icons (MIT)
// ══════════════════════════════════════════════════════

const _BRAND_ICONS = {
  claude: '<svg viewBox="0 0 24 24"><path d="M4.709 15.955l4.72-2.647.08-.23-.08-.128H9.2l-.79-.048-2.698-.073-2.339-.097-2.266-.122-.571-.121L0 11.784l.055-.352.48-.321.686.06 1.52.103 2.278.158 1.652.097 2.449.255h.389l.055-.157-.134-.098-.103-.097-2.358-1.596-2.552-1.688-1.336-.972-.724-.491-.364-.462-.158-1.008.656-.722.881.06.225.061.893.686 1.908 1.476 2.491 1.833.365.304.145-.103.019-.073-.164-.274-1.355-2.446-1.446-2.49-.644-1.032-.17-.619a2.97 2.97 0 01-.104-.729L6.283.134 6.696 0l.996.134.42.364.62 1.414 1.002 2.229 1.555 3.03.456.898.243.832.091.255h.158V9.01l.128-1.706.237-2.095.23-2.695.08-.76.376-.91.747-.492.584.28.48.685-.067.444-.286 1.851-.559 2.903-.364 1.942h.212l.243-.242.985-1.306 1.652-2.064.73-.82.85-.904.547-.431h1.033l.76 1.129-.34 1.166-1.064 1.347-.881 1.142-1.264 1.7-.79 1.36.073.11.188-.02 2.856-.606 1.543-.28 1.841-.315.833.388.091.395-.328.807-1.969.486-2.309.462-3.439.813-.042.03.049.061 1.549.146.662.036h1.622l3.02.225.79.522.474.638-.079.485-1.215.62-1.64-.389-3.829-.91-1.312-.329h-.182v.11l1.093 1.068 2.006 1.81 2.509 2.33.127.578-.322.455-.34-.049-2.205-1.657-.851-.747-1.926-1.62h-.128v.17l.444.649 2.345 3.521.122 1.08-.17.353-.608.213-.668-.122-1.374-1.925-1.415-2.167-1.143-1.943-.14.08-.674 7.254-.316.37-.729.28-.607-.461-.322-.747.322-1.476.389-1.924.315-1.53.286-1.9.17-.632-.012-.042-.14.018-1.434 1.967-2.18 2.945-1.726 1.845-.414.164-.717-.37.067-.662.401-.589 2.388-3.036 1.44-1.882.93-1.086-.006-.158h-.055L4.132 18.56l-1.13.146-.487-.456.061-.746.231-.243 1.908-1.312-.006.006z"/></svg>',
  openai: '<svg viewBox="0 0 24 24"><path d="M9.205 8.658v-2.26c0-.19.072-.333.238-.428l4.543-2.616c.619-.357 1.356-.523 2.117-.523 2.854 0 4.662 2.212 4.662 4.566 0 .167 0 .357-.024.547l-4.71-2.759a.797.797 0 00-.856 0l-5.97 3.473zm10.609 8.8V12.06c0-.333-.143-.57-.429-.737l-5.97-3.473 1.95-1.118a.433.433 0 01.476 0l4.543 2.617c1.309.76 2.189 2.378 2.189 3.948 0 1.808-1.07 3.473-2.76 4.163zM7.802 12.703l-1.95-1.142c-.167-.095-.239-.238-.239-.428V5.899c0-2.545 1.95-4.472 4.591-4.472 1 0 1.927.333 2.712.928L8.23 5.067c-.285.166-.428.404-.428.737v6.898zM12 15.128l-2.795-1.57v-3.33L12 8.658l2.795 1.57v3.33L12 15.128zm1.796 7.23c-1 0-1.927-.332-2.712-.927l4.686-2.712c.285-.166.428-.404.428-.737v-6.898l1.974 1.142c.167.095.238.238.238.428v5.233c0 2.545-1.974 4.472-4.614 4.472zm-5.637-5.303l-4.544-2.617c-1.308-.761-2.188-2.378-2.188-3.948A4.482 4.482 0 014.21 6.327v5.423c0 .333.143.571.428.738l5.947 3.449-1.95 1.118a.432.432 0 01-.476 0zm-.262 3.9c-2.688 0-4.662-2.021-4.662-4.519 0-.19.024-.38.047-.57l4.686 2.71c.286.167.571.167.856 0l5.97-3.448v2.26c0 .19-.07.333-.237.428l-4.543 2.616c-.619.357-1.356.523-2.117.523zm5.899 2.83a5.947 5.947 0 005.827-4.756C22.287 18.339 24 15.84 24 13.296c0-1.665-.713-3.282-1.998-4.448.119-.5.19-.999.19-1.498 0-3.401-2.759-5.947-5.946-5.947-.642 0-1.26.095-1.88.31A5.962 5.962 0 0010.205 0a5.947 5.947 0 00-5.827 4.757C1.713 5.447 0 7.945 0 10.49c0 1.666.713 3.283 1.998 4.448-.119.5-.19 1-.19 1.499 0 3.401 2.759 5.946 5.946 5.946.642 0 1.26-.095 1.88-.309a5.96 5.96 0 004.162 1.713z"/></svg>',
  gemini: '<svg viewBox="0 0 24 24"><path d="M20.616 10.835a14.147 14.147 0 01-4.45-3.001 14.111 14.111 0 01-3.678-6.452.503.503 0 00-.975 0 14.134 14.134 0 01-3.679 6.452 14.155 14.155 0 01-4.45 3.001c-.65.28-1.318.505-2.002.678a.502.502 0 000 .975c.684.172 1.35.397 2.002.677a14.147 14.147 0 014.45 3.001 14.112 14.112 0 013.679 6.453.502.502 0 00.975 0c.172-.685.397-1.351.677-2.003a14.145 14.145 0 013.001-4.45 14.113 14.113 0 016.453-3.678.503.503 0 000-.975 13.245 13.245 0 01-2.003-.678z"/></svg>',
  qwen: '<svg viewBox="0 0 24 24"><path d="M12.604 1.34c.393.69.784 1.382 1.174 2.075a.18.18 0 00.157.091h5.552c.174 0 .322.11.446.327l1.454 2.57c.19.337.24.478.024.837-.26.43-.513.864-.76 1.3l-.367.658c-.106.196-.223.28-.04.512l2.652 4.637c.172.301.111.494-.043.77-.437.785-.882 1.564-1.335 2.34-.159.272-.352.375-.68.37-.777-.016-1.552-.01-2.327.016a.099.099 0 00-.081.05 575.097 575.097 0 01-2.705 4.74c-.169.293-.38.363-.725.364-.997.003-2.002.004-3.017.002a.537.537 0 01-.465-.271l-1.335-2.323a.09.09 0 00-.083-.049H4.982c-.285.03-.553-.001-.805-.092l-1.603-2.77a.543.543 0 01-.002-.54l1.207-2.12a.198.198 0 000-.197 550.951 550.951 0 01-1.875-3.272l-.79-1.395c-.16-.31-.173-.496.095-.965.465-.813.927-1.625 1.387-2.436.132-.234.304-.334.584-.335a338.3 338.3 0 012.589-.001.124.124 0 00.107-.063l2.806-4.895a.488.488 0 01.422-.246c.524-.001 1.053 0 1.583-.006L11.704 1c.341-.003.724.032.9.34zm-3.432.403a.06.06 0 00-.052.03L6.254 6.788a.157.157 0 01-.135.078H3.253c-.056 0-.07.025-.041.074l5.81 10.156c.025.042.013.062-.034.063l-2.795.015a.218.218 0 00-.2.116l-1.32 2.31c-.044.078-.021.118.068.118l5.716.008c.046 0 .08.02.104.061l1.403 2.454c.046.081.092.082.139 0l5.006-8.76.783-1.382a.055.055 0 01.096 0l1.424 2.53a.122.122 0 00.107.062l2.763-.02a.04.04 0 00.035-.02.041.041 0 000-.04l-2.9-5.086a.108.108 0 010-.113l.293-.507 1.12-1.977c.024-.041.012-.062-.035-.062H9.2c-.059 0-.073-.026-.043-.077l1.434-2.505a.107.107 0 000-.114L9.225 1.774a.06.06 0 00-.053-.031zm6.29 8.02c.046 0 .058.02.034.06l-.832 1.465-2.613 4.585a.056.056 0 01-.05.029.058.058 0 01-.05-.029L8.498 9.841c-.02-.034-.01-.052.028-.054l.216-.012 6.722-.012z"/></svg>',
  doubao: '<svg viewBox="0 0 24 24"><path d="M5.31 15.756c.172-3.75 1.883-5.999 2.549-6.739-3.26 2.058-5.425 5.658-6.358 8.308v1.12C1.501 21.513 4.226 24 7.59 24a6.59 6.59 0 002.2-.375c.353-.12.7-.248 1.039-.378.913-.899 1.65-1.91 2.243-2.992-4.877 2.431-7.974.072-7.763-4.5l.002.001z" opacity=".5"/><path d="M22.57 10.283c-1.212-.901-4.109-2.404-7.397-2.8.295 3.792.093 8.766-2.1 12.773a12.782 12.782 0 01-2.244 2.992c3.764-1.448 6.746-3.457 8.596-5.219 2.82-2.683 3.353-5.178 3.361-6.66a2.737 2.737 0 00-.216-1.084v-.002z"/><path d="M14.303 1.867C12.955.7 11.248 0 9.39 0 7.532 0 5.883.677 4.545 1.807 2.791 3.29 1.627 5.557 1.5 8.125v9.201c.932-2.65 3.097-6.25 6.357-8.307.5-.318 1.025-.595 1.569-.829 1.883-.801 3.878-.932 5.746-.706-.222-2.83-.718-5.002-.87-5.617h.001z"/><path d="M17.305 4.961a199.47 199.47 0 01-1.08-1.094c-.202-.213-.398-.419-.586-.622l-1.333-1.378c.151.615.648 2.786.869 5.617 3.288.395 6.185 1.898 7.396 2.8-1.306-1.275-3.475-3.487-5.266-5.323z" opacity=".5"/></svg>',
  minimax: '<svg viewBox="0 0 24 24"><path d="M16.278 2c1.156 0 2.093.927 2.093 2.07v12.501a.74.74 0 00.744.709.74.74 0 00.743-.709V9.099a2.06 2.06 0 012.071-2.049A2.06 2.06 0 0124 9.1v6.561a.649.649 0 01-.652.645.649.649 0 01-.653-.645V9.1a.762.762 0 00-.766-.758.762.762 0 00-.766.758v7.472a2.037 2.037 0 01-2.048 2.026 2.037 2.037 0 01-2.048-2.026v-12.5a.785.785 0 00-.788-.753.785.785 0 00-.789.752l-.001 15.904A2.037 2.037 0 0113.441 22a2.037 2.037 0 01-2.048-2.026V18.04c0-.356.292-.645.652-.645.36 0 .652.289.652.645v1.934c0 .263.142.506.372.638.23.131.514.131.744 0a.734.734 0 00.372-.638V4.07c0-1.143.937-2.07 2.093-2.07zm-5.674 0c1.156 0 2.093.927 2.093 2.07v11.523a.648.648 0 01-.652.645.648.648 0 01-.652-.645V4.07a.785.785 0 00-.789-.78.785.785 0 00-.789.78v14.013a2.06 2.06 0 01-2.07 2.048 2.06 2.06 0 01-2.071-2.048V9.1a.762.762 0 00-.766-.758.762.762 0 00-.766.758v3.8a2.06 2.06 0 01-2.071 2.049A2.06 2.06 0 010 12.9v-1.378c0-.357.292-.646.652-.646.36 0 .653.29.653.646V12.9c0 .418.343.757.766.757s.766-.339.766-.757V9.099a2.06 2.06 0 012.07-2.048 2.06 2.06 0 012.071 2.048v8.984c0 .419.343.758.767.758.423 0 .766-.339.766-.758V4.07c0-1.143.937-2.07 2.093-2.07z"/></svg>',
  deepseek: '<svg viewBox="0 0 24 24"><path d="M23.748 4.482c-.254-.124-.364.113-.512.234-.051.039-.094.09-.137.136-.372.397-.806.657-1.373.626-.829-.046-1.537.214-2.163.848-.133-.782-.575-1.248-1.247-1.548-.352-.156-.708-.311-.955-.65-.172-.241-.219-.51-.305-.774-.055-.16-.11-.323-.293-.35-.2-.031-.278.136-.356.276-.313.572-.434 1.202-.422 1.84.027 1.436.633 2.58 1.838 3.393.137.093.172.187.129.323-.082.28-.18.552-.266.833-.055.179-.137.217-.329.14a5.526 5.526 0 01-1.736-1.18c-.857-.828-1.631-1.742-2.597-2.458a11.365 11.365 0 00-.689-.471c-.985-.957.13-1.743.388-1.836.27-.098.093-.432-.779-.428-.872.004-1.67.295-2.687.684a3.055 3.055 0 01-.465.137 9.597 9.597 0 00-2.883-.102c-1.885.21-3.39 1.102-4.497 2.623C.082 8.606-.231 10.684.152 12.85c.403 2.284 1.569 4.175 3.36 5.653 1.858 1.533 3.997 2.284 6.438 2.14 1.482-.085 3.133-.284 4.994-1.86.47.234.962.327 1.78.397.63.059 1.236-.03 1.705-.128.735-.156.684-.837.419-.961-2.155-1.004-1.682-.595-2.113-.926 1.096-1.296 2.746-2.642 3.392-7.003.05-.347.007-.565 0-.845-.004-.17.035-.237.23-.256a4.173 4.173 0 001.545-.475c1.396-.763 1.96-2.015 2.093-3.517.02-.23-.004-.467-.247-.588zM11.581 18c-2.089-1.642-3.102-2.183-3.52-2.16-.392.024-.321.471-.235.763.09.288.207.486.371.739.114.167.192.416-.113.603-.673.416-1.842-.14-1.897-.167-1.361-.802-2.5-1.86-3.301-3.307-.774-1.393-1.224-2.887-1.298-4.482-.02-.386.093-.522.477-.592a4.696 4.696 0 011.529-.039c2.132.312 3.946 1.265 5.468 2.774.868.86 1.525 1.887 2.202 2.891.72 1.066 1.494 2.082 2.48 2.914.348.292.625.514.891.677-.802.09-2.14.11-3.054-.614zm1-6.44a.306.306 0 01.415-.287.302.302 0 01.2.288.306.306 0 01-.31.307.303.303 0 01-.304-.308zm3.11 1.596c-.2.081-.399.151-.59.16a1.245 1.245 0 01-.798-.254c-.274-.23-.47-.358-.552-.758a1.73 1.73 0 01.016-.588c.07-.327-.008-.537-.239-.727-.187-.156-.426-.199-.688-.199a.559.559 0 01-.254-.078c-.11-.054-.2-.19-.114-.358.028-.054.16-.186.192-.21.356-.202.767-.136 1.146.016.352.144.618.408 1.001.782.391.451.462.576.685.914.176.265.336.537.445.848.067.195-.019.354-.25.452z"/></svg>',
  grok: '<svg viewBox="0 0 24 24"><path d="M9.27 15.29l7.978-5.897c.391-.29.95-.177 1.137.272.98 2.369.542 5.215-1.41 7.169-1.951 1.954-4.667 2.382-7.149 1.406l-2.711 1.257c3.889 2.661 8.611 2.003 11.562-.953 2.341-2.344 3.066-5.539 2.388-8.42l.006.007c-.983-4.232.242-5.924 2.75-9.383.06-.082.12-.164.179-.248l-3.301 3.305v-.01L9.267 15.292M7.623 16.723c-2.792-2.67-2.31-6.801.071-9.184 1.761-1.763 4.647-2.483 7.166-1.425l2.705-1.25a7.808 7.808 0 00-1.829-1A8.975 8.975 0 005.984 5.83c-2.533 2.536-3.33 6.436-1.962 9.764 1.022 2.487-.653 4.246-2.34 6.022-.599.63-1.199 1.259-1.682 1.925l7.62-6.815"/></svg>',
  mistral: '<svg viewBox="0 0 24 24"><path clip-rule="evenodd" d="M3.428 3.4h3.429v3.428h3.429v3.429h-.002 3.431V6.828h3.427V3.4h3.43v13.714H24v3.429H13.714v-3.428h-3.428v-3.429h-3.43v3.428h3.43v3.429H0v-3.429h3.428V3.4zm10.286 13.715h3.428v-3.429h-3.427v3.429z"/></svg>',
  glm: '<svg viewBox="0 0 24 24" fill="currentColor" fill-rule="evenodd"><path d="M11.991 23.503a.24.24 0 00-.244.248.24.24 0 00.244.249.24.24 0 00.245-.249.24.24 0 00-.22-.247l-.025-.001zM9.671 5.365a1.697 1.697 0 011.099 2.132l-.071.172-.016.04-.018.054c-.07.16-.104.32-.104.498-.035.71.47 1.279 1.186 1.314h.366c1.309.053 2.338 1.173 2.286 2.523-.052 1.332-1.152 2.38-2.478 2.327h-.174c-.715.018-1.274.64-1.239 1.368 0 .124.018.23.053.337.209.373.54.658.96.8.75.23 1.517-.125 1.9-.782l.018-.035c.402-.64 1.17-.96 1.92-.711.854.284 1.378 1.226 1.099 2.167a1.661 1.661 0 01-2.077 1.102 1.711 1.711 0 01-.907-.711l-.017-.035c-.2-.323-.463-.58-.851-.711l-.056-.018a1.646 1.646 0 00-1.954.746 1.66 1.66 0 01-1.065.764 1.677 1.677 0 01-1.989-1.279c-.209-.906.332-1.83 1.257-2.043a1.51 1.51 0 01.296-.035h.018c.68-.071 1.151-.622 1.116-1.333a1.307 1.307 0 00-.227-.693 2.515 2.515 0 01-.366-1.403 2.39 2.39 0 01.366-1.208c.14-.195.21-.444.227-.693.018-.71-.506-1.261-1.186-1.332l-.07-.018a1.43 1.43 0 01-.299-.07l-.05-.019a1.7 1.7 0 01-1.047-2.114 1.68 1.68 0 012.094-1.101zm-5.575 10.11c.26-.264.639-.367.994-.27.355.096.633.379.728.74.095.362-.007.748-.267 1.013-.402.41-1.053.41-1.455 0a1.062 1.062 0 010-1.482zm14.845-.294c.359-.09.738.024.992.297.254.274.344.665.237 1.025-.107.36-.396.634-.756.718-.551.128-1.1-.22-1.23-.781a1.05 1.05 0 01.757-1.26zm-.064-4.39c.314.32.49.753.49 1.206 0 .452-.176.886-.49 1.206-.315.32-.74.5-1.185.5-.444 0-.87-.18-1.184-.5a1.727 1.727 0 010-2.412 1.654 1.654 0 012.369 0zm-11.243.163c.364.484.447 1.128.218 1.691a1.665 1.665 0 01-2.188.923c-.855-.36-1.26-1.358-.907-2.228a1.68 1.68 0 011.33-1.038c.593-.08 1.183.169 1.547.652zm11.545-4.221c.368 0 .708.2.892.524.184.324.184.724 0 1.048a1.026 1.026 0 01-.892.524c-.568 0-1.03-.47-1.03-1.048 0-.579.462-1.048 1.03-1.048zm-14.358 0c.368 0 .707.2.891.524.184.324.184.724 0 1.048a1.026 1.026 0 01-.891.524c-.569 0-1.03-.47-1.03-1.048 0-.579.461-1.048 1.03-1.048zm10.031-1.475c.925 0 1.675.764 1.675 1.706s-.75 1.705-1.675 1.705-1.674-.763-1.674-1.705c0-.942.75-1.706 1.674-1.706zm-2.626-.684c.362-.082.653-.356.761-.718a1.062 1.062 0 00-.238-1.028 1.017 1.017 0 00-.996-.294c-.547.14-.881.7-.752 1.257.13.558.675.907 1.225.783zm0 16.876c.359-.087.644-.36.75-.72a1.062 1.062 0 00-.237-1.019 1.018 1.018 0 00-.985-.301 1.037 1.037 0 00-.762.717c-.108.361-.017.754.239 1.028.245.263.606.377.953.305l.043-.01zM17.19 3.5a.631.631 0 00.628-.64c0-.355-.279-.64-.628-.64a.631.631 0 00-.628.64c0 .355.28.64.628.64zm-10.38 0a.631.631 0 00.628-.64c0-.355-.28-.64-.628-.64a.631.631 0 00-.628.64c0 .355.279.64.628.64zm-5.182 7.852a.631.631 0 00-.628.64c0 .354.28.639.628.639a.63.63 0 00.627-.606l.001-.034a.62.62 0 00-.628-.64zm5.182 9.13a.631.631 0 00-.628.64c0 .355.279.64.628.64a.631.631 0 00.628-.64c0-.355-.28-.64-.628-.64zm10.38.018a.631.631 0 00-.628.64c0 .355.28.64.628.64a.631.631 0 00.628-.64c0-.355-.279-.64-.628-.64zm5.182-9.148a.631.631 0 00-.628.64c0 .354.279.639.628.639a.631.631 0 00.628-.64c0-.355-.28-.64-.628-.64zm-.384-4.992a.24.24 0 00.244-.249.24.24 0 00-.244-.249.24.24 0 00-.244.249c0 .142.122.249.244.249zM11.991.497a.24.24 0 00.245-.248A.24.24 0 0011.99 0a.24.24 0 00-.244.249c0 .133.108.236.223.247l.021.001zM2.011 6.36a.24.24 0 00.245-.249.24.24 0 00-.244-.249.24.24 0 00-.244.249.24.24 0 00.244.249zm0 11.263a.24.24 0 00-.243.248.24.24 0 00.244.249.24.24 0 00.244-.249.252.252 0 00-.244-.248zm19.995-.018a.24.24 0 00-.245.248.24.24 0 00.245.25.24.24 0 00.244-.25.252.252 0 00-.244-.248z"/></svg>',
  meituan: '<svg viewBox="0 0 24 24"><path d="M6.923 0c-2.408 0-3.28.25-4.16.721A4.906 4.907 0 0 0 .722 2.763C.25 3.643 0 4.516 0 6.923v10.154c0 2.407.25 3.28.72 4.16a4.9 4.9 0 0 0 2.042 2.042c.88.47 1.752.721 4.16.721h10.156c2.407 0 3.28-.25 4.16-.721a4.906 4.907 0 0 0 2.04-2.042c.471-.88.722-1.753.722-4.16V6.923c0-2.407-.25-3.28-.722-4.16A4.906 4.907 0 0 0 21.238.72C20.357.251 19.484 0 17.077 0ZM4.17 7.51h1.084c.04.24.07.488.11.737h3.47c.05-.25.08-.497.1-.736h1.105a10 10 0 0 1-.09.736h1.562v.866H7.62v.696h3.642v.855h-3.64v.667h3.64v.854h-3.64v.816h3.89v.865H7.88c.775.935 2.218 1.532 3.78 1.651l-.538.936c-1.442-.17-3.103-.846-4.028-2.04c-.856 1.194-2.487 1.92-4.525 2.07l.318-1.005c1.382-.02 2.814-.736 3.431-1.612h-3.62v-.865h3.86v-.816h-3.64v-.854h3.64v-.667h-3.64v-.855h3.64v-.697H2.7v-.866h1.56zm8.603.182h7.976c.358 0 .567.198.567.547v8.146H13.33c-.358 0-.557-.199-.557-.547zm1.044.885V15.5h6.455V8.577Zm3.999.476h1.024v.756h.975v.835h-.975V13c0 .806-.1 1.402-.318 2.02h-1.113c.338-.717.408-1.224.408-1.99v-2.387h-.935c-.14 1.541-.736 3.451-1.363 4.376h-1.134c.607-.855 1.303-2.526 1.472-4.376h-1.512v-.835h3.472z"/></svg>',
  tsinghua: '<svg viewBox="0 0 24 24"><path fill-rule="evenodd" d="M0 12a12 12 0 1024 0 12 12 0 10-24 0zm1 0a11 11 0 1122 0 11 11 0 11-22 0zm1.8 0a9.2 9.2 0 1018.4 0 9.2 9.2 0 10-18.4 0zm.7 0a8.5 8.5 0 1117 0 8.5 8.5 0 11-17 0zm1.5 0a7 7 0 1014 0 7 7 0 10-14 0zm.7 0a6.3 6.3 0 1112.6 0 6.3 6.3 0 11-12.6 0z"/><polygon points="12,8.8 12.8,11 15,11 13.2,12.4 13.9,14.6 12,13.3 10.1,14.6 10.8,12.4 9,11 11.2,11"/></svg>',
  openrouter: '<svg viewBox="0 0 24 24"><path d="M16.804 1.957l7.22 4.105v.087L16.73 10.21l.017-2.117-.821-.03c-1.059-.028-1.611.002-2.268.11-1.064.175-2.038.577-3.147 1.352L8.345 11.03c-.284.195-.495.336-.68.455l-.515.322-.397.234.385.23.53.338c.476.314 1.17.796 2.701 1.866 1.11.775 2.083 1.177 3.147 1.352l.3.045c.694.091 1.375.094 2.825.033l.022-2.159 7.22 4.105v.087L16.589 22l.014-1.862-.635.022c-1.386.042-2.137.002-3.138-.162-1.694-.28-3.26-.926-4.881-2.059l-2.158-1.5a21.997 21.997 0 00-.755-.498l-.467-.28a55.927 55.927 0 00-.76-.43C2.908 14.73.563 14.116 0 14.116V9.888l.14.004c.564-.007 2.91-.622 3.809-1.124l1.016-.58.438-.274c.428-.28 1.072-.726 2.686-1.853 1.621-1.133 3.186-1.78 4.881-2.059 1.152-.19 1.974-.213 3.814-.138l.02-1.907z"/></svg>',
  mimo: '<svg viewBox="0 0 24 24"><path d="M.958 15.936a.459.459 0 01.459.44v2.729a.46.46 0 01-.918 0v-2.729a.459.459 0 01.459-.44zm4.814-2.035a.46.46 0 01.553.45v4.754a.458.458 0 11-.918 0V15.48L3.74 17.202a.462.462 0 01-.655.016.462.462 0 01-.065-.082L.628 14.67a.459.459 0 01.658-.637l2.124 2.187 2.127-2.188a.46.46 0 01.235-.13zm2.068.004a.46.46 0 01.458.445v4.755a.46.46 0 01-.458.458.459.459 0 01-.458-.458V14.35a.459.459 0 01.458-.445zm1.973 2.014a.46.46 0 01.46.457v2.729a.46.46 0 01-.784.324.46.46 0 01-.134-.324v-2.729a.46.46 0 01.458-.458zm.002-2.045a.458.458 0 01.328.157l2.127 2.19 2.125-2.19a.459.459 0 01.784.318v4.756a.46.46 0 01-.455.458.46.46 0 01-.458-.458V15.48l-1.667 1.723a.46.46 0 01-.65.008l-.005-.005c0-.002-.002-.002-.004-.003l-2.455-2.534a.46.46 0 01-.008-.667.461.461 0 01.338-.128zm6.797 1.206a.46.46 0 01.53.651A1.966 1.966 0 0019.81 18.4a.462.462 0 01.623.18.46.46 0 01-.181.624 2.863 2.863 0 01-1.38.353l-.142-.004a2.88 2.88 0 01-2.393-4.263.461.461 0 01.274-.21zm.864-.931a2.884 2.884 0 013.915 3.914.46.46 0 01-.402.24l-.057-.004a.458.458 0 01-.164-.055.46.46 0 01-.182-.622 1.967 1.967 0 00-2.669-2.67.459.459 0 11-.441-.803zM9.59 6.368c1.481 0 1.696 1.202 1.696 1.654v2.648h-.917v-.432c-.26.346-.792.535-1.36.535-.133 0-1.289-.03-1.384-1.136-.082-.932.675-1.61 2.053-1.61h.691c0-.563-.367-.886-.983-.886-.44.013-.864.174-1.2.458l-.36-.664c.484-.379 1.012-.567 1.764-.567zm4.427.1c1.263 0 2.082.97 2.083 2.15 0 1.181-.824 2.154-2.083 2.154-1.26 0-2.084-.972-2.084-2.152 0-1.18.82-2.153 2.084-2.153zm6.801.015c.68 0 1.202.465 1.197 1.548v2.642H21.1V8.29c0-.312-.002-.98-.63-.98s-.628.667-.628.838v2.524h-.89V8.148c0-.17-.001-.838-.63-.838-.628 0-.628.668-.628.98v2.383h-.917v-4.03h.917V7a1.22 1.22 0 01.947-.516c.398 0 .76.193.982.686a1.321 1.321 0 011.195-.686zm-18.093.872l1.457-1.772H5.32L3.311 8.07l2.14 2.602H4.24L2.725 8.796 1.21 10.672H0L2.138 8.07.13 5.583h1.138l1.458 1.772zm4.149 3.317h-.916V6.644h.916v4.028zm16.99 0h-.916V6.644h.916v4.028zM9.925 8.71c-1.055 0-1.359.412-1.326.742.032.329.324.537.757.537a1.013 1.013 0 001.014-.968l.002-.31h-.447zM14.018 7.3c-.663 0-1.184.487-1.184 1.32 0 .832.52 1.32 1.184 1.32.662 0 1.182-.49 1.182-1.32 0-.832-.52-1.32-1.182-1.32zM6.417 5.001a.568.568 0 01.587.582.588.588 0 01-1.175 0A.57.57 0 016.417 5zm16.991 0a.57.57 0 01.592.582.588.588 0 01-1.174 0 .57.57 0 01.357-.542.572.572 0 01.225-.04z"/></svg>',
  generic: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="3" y="3" width="18" height="18" rx="4"/><circle cx="9" cy="10" r="1.5" fill="currentColor" stroke="none"/><circle cx="15" cy="10" r="1.5" fill="currentColor" stroke="none"/><path d="M8.5 15.5c1 1.5 6 1.5 7 0" stroke-linecap="round"/></svg>',
};

// Brand colors for CSS inline styling (provider icons)
const _BRAND_COLORS = {
  claude: '#D97706', openai: '#10a37f', gemini: '#4285F4', qwen: '#6F42C1',
  doubao: '#3B82F6', minimax: '#ef4444', deepseek: '#4D6BFE', grok: '#aaa',
  mistral: '#F7D046', glm: '#3859FF', meituan: '#FFC300', tsinghua: '#82318E',
  openrouter: '#6566F1', mimo: '#FF6A00', generic: '#888',
};

const _BRAND_PATTERNS = [
  // ★ Aggregator platforms MUST be before individual model brands — they host
  //   Claude/GPT/etc. models, so the combined brand hint would match those first.
  [/yeysai|thunlp|tsinghua|清华/i,           'tsinghua'],
  // [/your-org-pattern/i, 'your-org'],  // Add org detection pattern
  [/openrouter/i,                          'openrouter'],
  [/mimo|xiaomi/i,                         'mimo'],
  [/claude|anthropic|opus|sonnet|haiku/i, 'claude'],
  [/gpt|openai|o[134]-|chatgpt|dall/i,   'openai'],
  [/gemini|gemma|palm|bard/i,             'gemini'],
  [/qwen|tongyi|qwq/i,                    'qwen'],
  [/doubao|seed.*pro|byte/i,              'doubao'],
  [/minimax|abab|m2-her/i,                  'minimax'],
  [/deepseek/i,                            'deepseek'],
  [/grok|xai/i,                            'grok'],
  [/mistral|mixtral|pixtral/i,             'mistral'],
  [/glm|zhipu|z\.ai|chatglm|bigmodel/i,   'glm'],
];

function _detectBrand(text) {
  if (!text) return 'generic';
  for (var i = 0; i < _BRAND_PATTERNS.length; i++) {
    if (_BRAND_PATTERNS[i][0].test(text)) return _BRAND_PATTERNS[i][1];
  }
  return 'generic';
}

/* ★ Short display name for a model_id — available globally for ui.js, main.js etc. */
function _modelShortName(modelId) {
  if (!modelId) return 'Model';
  if (typeof _modelPricingCache !== 'undefined' && _modelPricingCache && _modelPricingCache[modelId]) {
    var mp = _modelPricingCache[modelId];
    if (mp.name) return mp.name;
  }
  return modelId.replace(/^(aws\.|vertex\.)/, '').split('/').pop();
}

function _brandSvg(brand, size) {
  var s = size || 20;
  var svg = _BRAND_ICONS[brand] || _BRAND_ICONS.generic;
  var color = _BRAND_COLORS[brand] || _BRAND_COLORS.generic;
  return '<span class="stg-brand-icon" style="width:' + s + 'px;height:' + s + 'px;color:' + color + '">' + svg + '</span>';
}

// ── Tofu mascot avatars — all rendered as <img> referencing SVG files ──
// Each file in static/icons/ can be browsed/previewed directly.
// Planner/Critic/Worker are AI-generated PNGs converted to SVG via vtracer.
// Default agent and user (onigiri) are hand-crafted compact SVGs.
//
// Icon files:
//   tofu-planner.svg  — tofu with beret + clipboard (planner role)
//   tofu-critic.svg   — tofu with monocle + magnifying glass (critic role)
//   tofu-worker.svg   — tofu with hard hat + wrench (worker role)
//   onigiri.svg       — rice ball mascot (user avatar)
//
// Generate new icons: python3 scripts/gen_tofu_icons.py
// Convert PNG→SVG:    python3 scripts/png_to_svg.py

const _ICON_V = '0.6.1';  // cache-bust version — bump when icons change
const _ICON_BASE = (typeof BASE_PATH!=='undefined'?BASE_PATH:'') + '/static/icons';

const _TOFU_PLANNER_SVG = `<img src="${_ICON_BASE}/tofu-planner.svg?v=${_ICON_V}" alt="Planner" style="width:100%;height:100%;display:block">`;
const _TOFU_CRITIC_SVG  = `<img src="${_ICON_BASE}/tofu-critic.svg?v=${_ICON_V}" alt="Critic" style="width:100%;height:100%;display:block">`;
const _TOFU_WORKER_SVG  = `<img src="${_ICON_BASE}/tofu-worker.svg?v=${_ICON_V}" alt="Worker" style="width:100%;height:100%;display:block">`;
const _USER_AVATAR_SVG  = `<img src="${_ICON_BASE}/onigiri.svg?v=${_ICON_V}" alt="You" style="width:100%;height:100%;display:block">`;

// ══════════════════════════════════════════════════════
//  Provider Templates — pre-configured public LLM providers
// ══════════════════════════════════════════════════════

const _PROVIDER_TEMPLATES = [
  {
    key: 'minimax', brand: 'minimax',
    name: 'MiniMax',
    base_url: 'https://api.minimax.chat/v1',
    balance_url: '',
    models: [
      { model_id: 'MiniMax-M2.7',           capabilities: ['text', 'thinking'],  rpm: 60,  cost: 0.001 },
      { model_id: 'MiniMax-M2.7-highspeed', capabilities: ['text', 'thinking'],  rpm: 60,  cost: 0.001 },
      { model_id: 'MiniMax-M2.5',           capabilities: ['text', 'thinking'],  rpm: 60,  cost: 0.001 },
      { model_id: 'MiniMax-M2.5-highspeed', capabilities: ['text', 'thinking'],  rpm: 60,  cost: 0.001 },
      { model_id: 'MiniMax-M2.1',           capabilities: ['text', 'thinking'],  rpm: 60,  cost: 0.001 },
      { model_id: 'MiniMax-M2.1-highspeed', capabilities: ['text', 'thinking'],  rpm: 60,  cost: 0.002 },
      { model_id: 'MiniMax-M2',             capabilities: ['text', 'vision'],    rpm: 60,  cost: 0.001 },
      { model_id: 'M2-her',                 capabilities: ['text'],              rpm: 60,  cost: 0.001 },
    ],
  },
  {
    key: 'glm', brand: 'glm',
    name: 'GLM (Zhipu AI)',
    base_url: 'https://open.bigmodel.cn/api/paas/v4',
    balance_url: 'https://open.bigmodel.cn/api/paas/v4/dashboard/billing/subscription',
    models: [
      { model_id: 'glm-5.1',         capabilities: ['text', 'thinking'],  rpm: 60,  cost: 0.004 },
      { model_id: 'glm-5',           capabilities: ['text', 'thinking'],  rpm: 60,  cost: 0.004 },
      { model_id: 'glm-4.7',         capabilities: ['text', 'thinking'],  rpm: 60,  cost: 0.002 },
      { model_id: 'glm-4.5-air',     capabilities: ['text', 'cheap'],     rpm: 120, cost: 0.001 },
      { model_id: 'glm-4.5-flash',   capabilities: ['text', 'cheap'],     rpm: 200, cost: 0.0 },
    ],
  },
  {
    key: 'deepseek', brand: 'deepseek',
    name: 'DeepSeek',
    base_url: 'https://api.deepseek.com',
    balance_url: 'https://api.deepseek.com/user/balance',
    models: [
      { model_id: 'deepseek-chat',     capabilities: ['text'],              rpm: 60,  cost: 0.001 },
      { model_id: 'deepseek-reasoner', capabilities: ['text', 'thinking'],  rpm: 30,  cost: 0.002 },
    ],
  },
  {
    key: 'openai', brand: 'openai',
    name: 'OpenAI',
    base_url: 'https://api.openai.com/v1',
    balance_url: 'https://api.openai.com/v1/dashboard/billing/subscription',
    models: [
      { model_id: 'gpt-5.4',       capabilities: ['text', 'vision', 'thinking'],   rpm: 30,  cost: 0.015 },
      { model_id: 'gpt-5.4-mini',  capabilities: ['text', 'vision', 'thinking'],   rpm: 60,  cost: 0.005 },
      { model_id: 'gpt-5.4-nano',  capabilities: ['text', 'vision', 'cheap'],      rpm: 200, cost: 0.001 },
      { model_id: 'o3',            capabilities: ['text', 'vision', 'thinking'],   rpm: 30,  cost: 0.010 },
      { model_id: 'o4-mini',       capabilities: ['text', 'vision', 'thinking'],   rpm: 30,  cost: 0.005 },
      { model_id: 'gpt-4.1',       capabilities: ['text', 'vision'],               rpm: 30,  cost: 0.010 },
      { model_id: 'gpt-4.1-mini',  capabilities: ['text', 'vision', 'cheap'],      rpm: 60,  cost: 0.002 },
    ],
  },
  {
    key: 'anthropic', brand: 'claude',
    name: 'Anthropic',
    base_url: 'https://api.anthropic.com/v1',
    balance_url: 'https://api.anthropic.com/v1/dashboard/billing/subscription',
    models: [
      { model_id: 'claude-opus-4-6',            capabilities: ['text', 'vision', 'thinking'], rpm: 30,  cost: 0.025 },
      { model_id: 'claude-sonnet-4-6',           capabilities: ['text', 'vision', 'thinking'], rpm: 50,  cost: 0.015 },
      { model_id: 'claude-haiku-4-5',            capabilities: ['text', 'vision', 'cheap'],    rpm: 100, cost: 0.005 },
      { model_id: 'claude-sonnet-4-20250514',    capabilities: ['text', 'vision', 'thinking'], rpm: 50,  cost: 0.009 },
    ],
  },
  {
    key: 'doubao', brand: 'doubao',
    name: 'Doubao (Volcengine)',
    base_url: 'https://ark.cn-beijing.volces.com/api/v3',
    balance_url: 'https://ark.cn-beijing.volces.com/api/v3/dashboard/billing/subscription',
    models: [
      { model_id: 'Doubao-Seed-2.0-pro',    capabilities: ['text', 'vision', 'thinking'], rpm: 60,  cost: 0.002 },
      { model_id: 'Doubao-Seed-2.0-lite',   capabilities: ['text', 'cheap'],              rpm: 120, cost: 0.001 },
      { model_id: 'Doubao-Seed-2.0-mini',   capabilities: ['text', 'cheap'],              rpm: 200, cost: 0.001 },
    ],
  },
  {
    key: 'mistral', brand: 'mistral',
    name: 'Mistral AI',
    base_url: 'https://api.mistral.ai/v1',
    balance_url: 'https://api.mistral.ai/v1/dashboard/billing/subscription',
    models: [
      { model_id: 'mistral-large-latest',   capabilities: ['text', 'vision', 'thinking'], rpm: 30,  cost: 0.008 },
      { model_id: 'mistral-small-latest',   capabilities: ['text', 'cheap'],              rpm: 60,  cost: 0.001 },
      { model_id: 'codestral-latest',       capabilities: ['text'],                       rpm: 60,  cost: 0.003 },
    ],
  },
  {
    key: 'xai', brand: 'grok',
    name: 'xAI (Grok)',
    base_url: 'https://api.x.ai/v1',
    balance_url: 'https://api.x.ai/v1/dashboard/billing/subscription',
    models: [
      { model_id: 'grok-3',       capabilities: ['text', 'thinking'],          rpm: 30,  cost: 0.010 },
      { model_id: 'grok-3-mini',  capabilities: ['text', 'thinking', 'cheap'], rpm: 60,  cost: 0.003 },
    ],
  },
  {
    key: 'qwen', brand: 'qwen',
    name: 'Qwen (DashScope)',
    base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
    balance_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1/dashboard/billing/subscription',
    models: [
      { model_id: 'qwen3-max',              capabilities: ['text', 'thinking'],                rpm: 30,   cost: 0.004 },
      { model_id: 'qwen-plus',              capabilities: ['text', 'thinking'],                rpm: 60,   cost: 0.002 },
      { model_id: 'qwen-max',               capabilities: ['text'],                            rpm: 30,   cost: 0.004 },
      { model_id: 'qwq-plus',               capabilities: ['text', 'thinking'],                rpm: 60,   cost: 0.002 },
      { model_id: 'qwen-turbo',             capabilities: ['text', 'cheap'],                   rpm: 200,  cost: 0.001 },
      { model_id: 'MiniMax-M2.1',           capabilities: ['cheap', 'text'],                   rpm: 120,  cost: 0.0006 },
      { model_id: 'MiniMax-M2.5',           capabilities: ['cheap', 'text'],                   rpm: 120,  cost: 0.0006 },
      { model_id: 'MiniMax/MiniMax-M2.1',   capabilities: ['cheap', 'text'],                   rpm: 120,  cost: 0.0006 },
      { model_id: 'MiniMax/MiniMax-M2.5',   capabilities: ['cheap', 'text'],                   rpm: 120,  cost: 0.0006 },
      { model_id: 'MiniMax/MiniMax-M2.7',   capabilities: ['cheap', 'text'],                   rpm: 120,  cost: 0.0008 },
      { model_id: 'MiniMax/speech-02-hd',   capabilities: ['cheap', 'text'],                   rpm: 120,  cost: 0.002 },
      { model_id: 'MiniMax/speech-02-turbo', capabilities: ['cheap', 'text'],                   rpm: 200,  cost: 0.002 },
      { model_id: 'MiniMax/speech-2.8-hd',  capabilities: ['cheap', 'text'],                   rpm: 120,  cost: 0.002 },
      { model_id: 'MiniMax/speech-2.8-turbo', capabilities: ['cheap', 'text'],                   rpm: 200,  cost: 0.002 },
      { model_id: 'codeqwen1.5-7b-chat',    capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0079 },
      { model_id: 'deepseek-r1',            capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0016 },
      { model_id: 'deepseek-r1-distill-llama-70b', capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0008 },
      { model_id: 'deepseek-r1-distill-llama-8b', capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0008 },
      { model_id: 'deepseek-r1-distill-qwen-1.5b', capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0003 },
      { model_id: 'deepseek-r1-distill-qwen-14b', capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0003 },
      { model_id: 'deepseek-r1-distill-qwen-32b', capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0003 },
      { model_id: 'deepseek-r1-distill-qwen-7b', capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0003 },
      { model_id: 'deepseek-v3',            capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0003 },
      { model_id: 'deepseek-v3.1',          capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0003 },
      { model_id: 'deepseek-v3.2',          capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0003 },
      { model_id: 'glm-4.7',                capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0011 },
      { model_id: 'glm-5',                  capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0015 },
      { model_id: 'gui-plus',               capabilities: ['text'],                            rpm: 60,   cost: 0.005 },
      { model_id: 'kimi-k2-thinking',       capabilities: ['cheap', 'text', 'thinking'],       rpm: 60,   cost: 0.0012 },
      { model_id: 'kimi-k2.5',              capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0011 },
      { model_id: 'kimi/kimi-k2.5',         capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0011 },
      { model_id: 'qvq-max',                capabilities: ['cheap', 'text'],                   rpm: 30,   cost: 0.0028 },
      { model_id: 'qvq-max-2025-05-15',     capabilities: ['text'],                            rpm: 30,   cost: 0.02 },
      { model_id: 'qvq-plus',               capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0005 },
      { model_id: 'qvq-plus-2025-05-15',    capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0009 },
      { model_id: 'qwen-1.8b-chat',         capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0056 },
      { model_id: 'qwen-1.8b-longcontext-chat', capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0056 },
      { model_id: 'qwen-14b-chat',          capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0001 },
      { model_id: 'qwen-72b-chat',          capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0008 },
      { model_id: 'qwen-7b-chat',           capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0001 },
      { model_id: 'qwen-coder-plus',        capabilities: ['cheap', 'text', 'vision'],         rpm: 60,   cost: 0.0019 },
      { model_id: 'qwen-coder-plus-1106',   capabilities: ['cheap', 'text', 'vision'],         rpm: 60,   cost: 0.0019 },
      { model_id: 'qwen-coder-plus-latest', capabilities: ['cheap', 'text', 'vision'],         rpm: 60,   cost: 0.0019 },
      { model_id: 'qwen-coder-turbo',       capabilities: ['cheap', 'text'],                   rpm: 200,  cost: 0.0004 },
      { model_id: 'qwen-coder-turbo-0919',  capabilities: ['cheap', 'text'],                   rpm: 200,  cost: 0.0004 },
      { model_id: 'qwen-coder-turbo-latest', capabilities: ['cheap', 'text'],                   rpm: 200,  cost: 0.0004 },
      { model_id: 'qwen-deep-research-2025-12-15', capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0009 },
      { model_id: 'qwen-deep-search-planning', capabilities: ['text'],                            rpm: 60,   cost: 0.005 },
      { model_id: 'qwen-flash',             capabilities: ['cheap', 'text'],                   rpm: 100,  cost: 0.0002 },
      { model_id: 'qwen-flash-character',   capabilities: ['cheap', 'text'],                   rpm: 100,  cost: 0.0002 },
      { model_id: 'qwen-flash-character-2026-02-26', capabilities: ['cheap', 'text'],                   rpm: 100,  cost: 0.0002 },
      { model_id: 'qwen-long',              capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0002 },
      { model_id: 'qwen-math-plus',         capabilities: ['cheap', 'text', 'vision'],         rpm: 60,   cost: 0.0009 },
      { model_id: 'qwen-math-plus-0919',    capabilities: ['cheap', 'text', 'vision'],         rpm: 60,   cost: 0.0009 },
      { model_id: 'qwen-math-plus-latest',  capabilities: ['cheap', 'text', 'vision'],         rpm: 60,   cost: 0.0009 },
      { model_id: 'qwen-math-turbo',        capabilities: ['cheap', 'text'],                   rpm: 200,  cost: 0.0001 },
      { model_id: 'qwen-math-turbo-0919',   capabilities: ['cheap', 'text'],                   rpm: 200,  cost: 0.0001 },
      { model_id: 'qwen-math-turbo-latest', capabilities: ['cheap', 'text'],                   rpm: 200,  cost: 0.0001 },
      { model_id: 'qwen-max-0107',          capabilities: ['cheap', 'text', 'vision'],         rpm: 30,   cost: 0.0023 },
      { model_id: 'qwen-max-0428',          capabilities: ['cheap', 'text', 'vision'],         rpm: 30,   cost: 0.0023 },
      { model_id: 'qwen-max-0919',          capabilities: ['cheap', 'text', 'vision'],         rpm: 30,   cost: 0.0023 },
      { model_id: 'qwen-max-1201',          capabilities: ['cheap', 'text', 'vision'],         rpm: 30,   cost: 0.0023 },
      { model_id: 'qwen-max-2025-01-25',    capabilities: ['cheap', 'text', 'vision'],         rpm: 30,   cost: 0.0023 },
      { model_id: 'qwen-max-latest',        capabilities: ['cheap', 'text', 'vision'],         rpm: 30,   cost: 0.0023 },
      { model_id: 'qwen-max-longcontext',   capabilities: ['cheap', 'text', 'vision'],         rpm: 30,   cost: 0.0023 },
      { model_id: 'qwen-mt-flash',          capabilities: ['cheap', 'text'],                   rpm: 100,  cost: 0.0002 },
      { model_id: 'qwen-mt-lite',           capabilities: ['cheap', 'text'],                   rpm: 120,  cost: 0.002 },
      { model_id: 'qwen-mt-plus',           capabilities: ['cheap', 'text', 'vision'],         rpm: 60,   cost: 0.0009 },
      { model_id: 'qwen-mt-turbo',          capabilities: ['cheap', 'text'],                   rpm: 200,  cost: 0.0001 },
      { model_id: 'qwen-omni-turbo',        capabilities: ['cheap', 'text'],                   rpm: 200,  cost: 0.0001 },
      { model_id: 'qwen-plus-2025-01-25',   capabilities: ['cheap', 'text', 'vision'],         rpm: 60,   cost: 0.0005 },
      { model_id: 'qwen-plus-2025-04-28',   capabilities: ['cheap', 'text', 'vision'],         rpm: 60,   cost: 0.0005 },
      { model_id: 'qwen-plus-2025-07-14',   capabilities: ['cheap', 'text', 'vision'],         rpm: 60,   cost: 0.0005 },
      { model_id: 'qwen-plus-2025-09-11',   capabilities: ['cheap', 'text', 'vision'],         rpm: 60,   cost: 0.0005 },
      { model_id: 'qwen-plus-2025-11-05',   capabilities: ['cheap', 'text', 'vision'],         rpm: 60,   cost: 0.0005 },
      { model_id: 'qwen-plus-2025-12-01',   capabilities: ['cheap', 'text', 'vision'],         rpm: 60,   cost: 0.0005 },
      { model_id: 'qwen-plus-latest',       capabilities: ['cheap', 'text', 'vision'],         rpm: 60,   cost: 0.0009 },
      { model_id: 'qwen-tts-2025-05-22',    capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0005 },
      { model_id: 'qwen-turbo-0919',        capabilities: ['cheap', 'text'],                   rpm: 200,  cost: 0.0001 },
      { model_id: 'qwen-turbo-2024-11-01',  capabilities: ['cheap', 'text'],                   rpm: 200,  cost: 0.0001 },
      { model_id: 'qwen-turbo-2025-04-28',  capabilities: ['cheap', 'text'],                   rpm: 200,  cost: 0.0005 },
      { model_id: 'qwen-turbo-2025-07-15',  capabilities: ['cheap', 'text'],                   rpm: 200,  cost: 0.0005 },
      { model_id: 'qwen-turbo-latest',      capabilities: ['cheap', 'text'],                   rpm: 200,  cost: 0.0001 },
      { model_id: 'qwen-vl-max',            capabilities: ['cheap', 'text', 'vision'],         rpm: 30,   cost: 0.0013 },
      { model_id: 'qwen-vl-max-2025-04-02', capabilities: ['cheap', 'text', 'vision'],         rpm: 30,   cost: 0.0013 },
      { model_id: 'qwen-vl-max-2025-04-08', capabilities: ['cheap', 'text', 'vision'],         rpm: 30,   cost: 0.0013 },
      { model_id: 'qwen-vl-max-latest',     capabilities: ['cheap', 'text', 'vision'],         rpm: 30,   cost: 0.0013 },
      { model_id: 'qwen-vl-ocr',            capabilities: ['cheap', 'text', 'vision'],         rpm: 60,   cost: 0.0003 },
      { model_id: 'qwen-vl-ocr-2025-11-20', capabilities: ['cheap', 'text', 'vision'],         rpm: 60,   cost: 0.0003 },
      { model_id: 'qwen-vl-ocr-latest',     capabilities: ['cheap', 'text', 'vision'],         rpm: 60,   cost: 0.0003 },
      { model_id: 'qwen-vl-plus',           capabilities: ['cheap', 'text', 'vision'],         rpm: 60,   cost: 0.0003 },
      { model_id: 'qwen-vl-plus-2025-01-25', capabilities: ['cheap', 'text', 'vision'],         rpm: 60,   cost: 0.0005 },
      { model_id: 'qwen-vl-plus-2025-05-07', capabilities: ['cheap', 'text', 'vision'],         rpm: 60,   cost: 0.0005 },
      { model_id: 'qwen-vl-plus-2025-08-15', capabilities: ['cheap', 'text', 'vision'],         rpm: 60,   cost: 0.0009 },
      { model_id: 'qwen-vl-plus-latest',    capabilities: ['cheap', 'text', 'vision'],         rpm: 60,   cost: 0.0003 },
      { model_id: 'qwen1.5-0.5b-chat',      capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0079 },
      { model_id: 'qwen1.5-1.8b-chat',      capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0056 },
      { model_id: 'qwen1.5-110b-chat',      capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0079 },
      { model_id: 'qwen1.5-14b-chat',       capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0079 },
      { model_id: 'qwen1.5-32b-chat',       capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0079 },
      { model_id: 'qwen1.5-72b-chat',       capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0079 },
      { model_id: 'qwen1.5-7b-chat',        capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0079 },
      { model_id: 'qwen2-0.5b-instruct',    capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0001 },
      { model_id: 'qwen2-1.5b-instruct',    capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0004 },
      { model_id: 'qwen2-57b-a14b-instruct', capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0001 },
      { model_id: 'qwen2-7b-instruct',      capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0001 },
      { model_id: 'qwen2.5-0.5b-instruct',  capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0001 },
      { model_id: 'qwen2.5-1.5b-instruct',  capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0001 },
      { model_id: 'qwen2.5-14b-instruct',   capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0001 },
      { model_id: 'qwen2.5-14b-instruct-1m', capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0001 },
      { model_id: 'qwen2.5-32b-instruct',   capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0004 },
      { model_id: 'qwen2.5-3b-instruct',    capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0001 },
      { model_id: 'qwen2.5-72b-instruct',   capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0008 },
      { model_id: 'qwen2.5-7b-instruct',    capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0001 },
      { model_id: 'qwen2.5-7b-instruct-1m', capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0001 },
      { model_id: 'qwen2.5-coder-14b-instruct', capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0001 },
      { model_id: 'qwen2.5-coder-32b-instruct', capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0001 },
      { model_id: 'qwen2.5-coder-7b-instruct', capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0001 },
      { model_id: 'qwen2.5-math-1.5b-instruct', capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0001 },
      { model_id: 'qwen2.5-math-72b-instruct', capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0008 },
      { model_id: 'qwen2.5-math-7b-instruct', capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0001 },
      { model_id: 'qwen2.5-vl-32b-instruct', capabilities: ['cheap', 'text', 'vision'],         rpm: 60,   cost: 0.0004 },
      { model_id: 'qwen3-0.6b',             capabilities: ['text'],                            rpm: 60,   cost: 0.005 },
      { model_id: 'qwen3-1.7b',             capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0002 },
      { model_id: 'qwen3-14b',              capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0001 },
      { model_id: 'qwen3-235b-a22b',        capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0011 },
      { model_id: 'qwen3-235b-a22b-instruct-2507', capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0005 },
      { model_id: 'qwen3-235b-a22b-thinking-2507', capabilities: ['cheap', 'text', 'thinking'],       rpm: 60,   cost: 0.0008 },
      { model_id: 'qwen3-30b-a3b',          capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0002 },
      { model_id: 'qwen3-30b-a3b-instruct-2507', capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0002 },
      { model_id: 'qwen3-30b-a3b-thinking-2507', capabilities: ['cheap', 'text', 'thinking'],       rpm: 60,   cost: 0.0002 },
      { model_id: 'qwen3-32b',              capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0002 },
      { model_id: 'qwen3-4b',               capabilities: ['text'],                            rpm: 60,   cost: 0.005 },
      { model_id: 'qwen3-8b',               capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0002 },
      { model_id: 'qwen3-asr-flash-2026-02-10', capabilities: ['cheap', 'text'],                   rpm: 100,  cost: 0.0002 },
      { model_id: 'qwen3-asr-flash-realtime', capabilities: ['cheap', 'text'],                   rpm: 100,  cost: 0.0002 },
      { model_id: 'qwen3-asr-flash-realtime-2025-10-27', capabilities: ['cheap', 'text'],                   rpm: 100,  cost: 0.0002 },
      { model_id: 'qwen3-asr-flash-realtime-2026-02-10', capabilities: ['cheap', 'text'],                   rpm: 100,  cost: 0.0002 },
      { model_id: 'qwen3-coder-480b-a35b-instruct', capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0002 },
      { model_id: 'qwen3-coder-flash',      capabilities: ['cheap', 'text'],                   rpm: 100,  cost: 0.0006 },
      { model_id: 'qwen3-coder-next',       capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0004 },
      { model_id: 'qwen3-coder-plus',       capabilities: ['cheap', 'text', 'vision'],         rpm: 60,   cost: 0.0019 },
      { model_id: 'qwen3-coder-plus-2025-07-22', capabilities: ['cheap', 'text', 'vision'],         rpm: 60,   cost: 0.0019 },
      { model_id: 'qwen3-coder-plus-2025-09-23', capabilities: ['cheap', 'text', 'vision'],         rpm: 60,   cost: 0.0019 },
      { model_id: 'qwen3-livetranslate-flash', capabilities: ['cheap', 'text'],                   rpm: 100,  cost: 0.0002 },
      { model_id: 'qwen3-livetranslate-flash-2025-12-01', capabilities: ['cheap', 'text'],                   rpm: 100,  cost: 0.0002 },
      { model_id: 'qwen3-livetranslate-flash-realtime', capabilities: ['cheap', 'text'],                   rpm: 100,  cost: 0.0002 },
      { model_id: 'qwen3-livetranslate-flash-realtime-2025-09-22', capabilities: ['cheap', 'text'],                   rpm: 100,  cost: 0.0003 },
      { model_id: 'qwen3-max-2025-09-23',   capabilities: ['cheap', 'text', 'vision'],         rpm: 30,   cost: 0.0002 },
      { model_id: 'qwen3-max-2026-01-23',   capabilities: ['cheap', 'text', 'vision'],         rpm: 30,   cost: 0.0002 },
      { model_id: 'qwen3-max-preview',      capabilities: ['cheap', 'text', 'vision'],         rpm: 30,   cost: 0.0023 },
      { model_id: 'qwen3-next-80b-a3b-instruct', capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0006 },
      { model_id: 'qwen3-next-80b-a3b-thinking', capabilities: ['cheap', 'text', 'thinking'],       rpm: 60,   cost: 0.0004 },
      { model_id: 'qwen3-omni-flash',       capabilities: ['cheap', 'text'],                   rpm: 100,  cost: 0.0002 },
      { model_id: 'qwen3-omni-flash-2025-09-15', capabilities: ['cheap', 'text'],                   rpm: 100,  cost: 0.0003 },
      { model_id: 'qwen3-omni-flash-2025-12-01', capabilities: ['cheap', 'text'],                   rpm: 100,  cost: 0.0002 },
      { model_id: 'qwen3-omni-flash-realtime', capabilities: ['cheap', 'text'],                   rpm: 100,  cost: 0.0002 },
      { model_id: 'qwen3-omni-flash-realtime-2025-09-15', capabilities: ['cheap', 'text'],                   rpm: 100,  cost: 0.0003 },
      { model_id: 'qwen3-omni-flash-realtime-2025-12-01', capabilities: ['cheap', 'text'],                   rpm: 100,  cost: 0.0002 },
      { model_id: 'qwen3-s2s-flash-realtime-2025-09-22', capabilities: ['cheap', 'text'],                   rpm: 100,  cost: 0.0003 },
      { model_id: 'qwen3-tts-flash',        capabilities: ['cheap', 'text'],                   rpm: 100,  cost: 0.0002 },
      { model_id: 'qwen3-tts-flash-2025-09-18', capabilities: ['cheap', 'text'],                   rpm: 100,  cost: 0.0003 },
      { model_id: 'qwen3-tts-flash-2025-11-27', capabilities: ['cheap', 'text'],                   rpm: 100,  cost: 0.0002 },
      { model_id: 'qwen3-tts-flash-realtime', capabilities: ['cheap', 'text'],                   rpm: 100,  cost: 0.0002 },
      { model_id: 'qwen3-tts-flash-realtime-2025-09-18', capabilities: ['cheap', 'text'],                   rpm: 100,  cost: 0.0003 },
      { model_id: 'qwen3-tts-flash-realtime-2025-11-27', capabilities: ['cheap', 'text'],                   rpm: 100,  cost: 0.0002 },
      { model_id: 'qwen3-tts-instruct-flash', capabilities: ['cheap', 'text'],                   rpm: 100,  cost: 0.0002 },
      { model_id: 'qwen3-tts-instruct-flash-2026-01-26', capabilities: ['cheap', 'text'],                   rpm: 100,  cost: 0.0002 },
      { model_id: 'qwen3-tts-instruct-flash-realtime', capabilities: ['cheap', 'text'],                   rpm: 100,  cost: 0.0002 },
      { model_id: 'qwen3-tts-instruct-flash-realtime-2026-01-22', capabilities: ['cheap', 'text'],                   rpm: 100,  cost: 0.0002 },
      { model_id: 'qwen3-tts-vc-2026-01-22', capabilities: ['text'],                            rpm: 60,   cost: 0.005 },
      { model_id: 'qwen3-tts-vc-realtime-2025-11-27', capabilities: ['text'],                            rpm: 60,   cost: 0.005 },
      { model_id: 'qwen3-tts-vc-realtime-2026-01-15', capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0009 },
      { model_id: 'qwen3-tts-vd-2026-01-26', capabilities: ['text'],                            rpm: 60,   cost: 0.005 },
      { model_id: 'qwen3-tts-vd-realtime-2025-12-16', capabilities: ['text'],                            rpm: 60,   cost: 0.005 },
      { model_id: 'qwen3-tts-vd-realtime-2026-01-15', capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0009 },
      { model_id: 'qwen3-vl-flash',         capabilities: ['cheap', 'text', 'vision'],         rpm: 100,  cost: 0.0002 },
      { model_id: 'qwen3-vl-flash-2025-10-15', capabilities: ['cheap', 'text', 'vision'],         rpm: 100,  cost: 0.0002 },
      { model_id: 'qwen3-vl-flash-2026-01-22', capabilities: ['cheap', 'text', 'vision'],         rpm: 100,  cost: 0.0002 },
      { model_id: 'qwen3-vl-plus',          capabilities: ['cheap', 'text', 'vision'],         rpm: 60,   cost: 0.0009 },
      { model_id: 'qwen3-vl-plus-2025-09-23', capabilities: ['cheap', 'text', 'vision'],         rpm: 60,   cost: 0.0002 },
      { model_id: 'qwen3-vl-plus-2025-12-19', capabilities: ['cheap', 'text', 'vision'],         rpm: 60,   cost: 0.0009 },
      { model_id: 'qwen3.5-122b-a10b',      capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0012 },
      { model_id: 'qwen3.5-27b',            capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0009 },
      { model_id: 'qwen3.5-35b-a3b',        capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0007 },
      { model_id: 'qwen3.5-397b-a17b',      capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0014 },
      { model_id: 'qwen3.5-flash',          capabilities: ['cheap', 'text'],                   rpm: 100,  cost: 0.0002 },
      { model_id: 'qwen3.5-flash-2026-02-23', capabilities: ['cheap', 'text'],                   rpm: 100,  cost: 0.0002 },
      { model_id: 'qwen3.5-plus',           capabilities: ['cheap', 'text', 'vision'],         rpm: 60,   cost: 0.0009 },
      { model_id: 'qwen3.5-plus-2026-02-15', capabilities: ['cheap', 'text', 'vision'],         rpm: 60,   cost: 0.0009 },
      { model_id: 'qwen3.6-plus',           capabilities: ['cheap', 'text', 'vision'],         rpm: 60,   cost: 0.0009 },
      { model_id: 'qwen3.6-plus-2026-04-02', capabilities: ['cheap', 'text', 'vision'],         rpm: 60,   cost: 0.0009 },
      { model_id: 'qwq-plus-2025-03-05',    capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0005 },
      { model_id: 'siliconflow/deepseek-r1-0528', capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0013 },
      { model_id: 'siliconflow/deepseek-v3-0324', capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0005 },
      { model_id: 'siliconflow/deepseek-v3.1-terminus', capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0005 },
      { model_id: 'siliconflow/deepseek-v3.2', capabilities: ['cheap', 'text'],                   rpm: 60,   cost: 0.0008 },
      { model_id: 'tongyi-xiaomi-analysis-flash', capabilities: ['cheap', 'text'],                   rpm: 100,  cost: 0.0002 },
      { model_id: 'tongyi-xiaomi-analysis-pro', capabilities: ['cheap', 'text'],                   rpm: 30,   cost: 0.002 },
      { model_id: 'qwen-image-2.0',         capabilities: ['cheap', 'image_gen'],              rpm: 10,   cost: 0.0011 },
      { model_id: 'qwen-image-2.0-2026-03-03', capabilities: ['cheap', 'image_gen'],              rpm: 10,   cost: 0.0011 },
      { model_id: 'qwen-image-2.0-pro',     capabilities: ['cheap', 'image_gen'],              rpm: 10,   cost: 0.0011 },
      { model_id: 'qwen-image-2.0-pro-2026-03-03', capabilities: ['cheap', 'image_gen'],              rpm: 10,   cost: 0.0011 },
      { model_id: 'qwen-image-edit-max',    capabilities: ['cheap', 'image_gen'],              rpm: 10,   cost: 0.0023 },
      { model_id: 'qwen-image-edit-max-2026-01-16', capabilities: ['cheap', 'image_gen'],              rpm: 10,   cost: 0.0023 },
      { model_id: 'qwen-image-edit-plus',   capabilities: ['cheap', 'image_gen'],              rpm: 10,   cost: 0.0009 },
      { model_id: 'qwen-image-edit-plus-2025-10-30', capabilities: ['cheap', 'image_gen'],              rpm: 10,   cost: 0.0005 },
      { model_id: 'qwen-image-edit-plus-2025-12-15', capabilities: ['cheap', 'image_gen'],              rpm: 10,   cost: 0.0009 },
      { model_id: 'qwen-image-max',         capabilities: ['cheap', 'image_gen'],              rpm: 10,   cost: 0.0023 },
      { model_id: 'qwen-image-max-2025-12-30', capabilities: ['cheap', 'image_gen'],              rpm: 10,   cost: 0.0023 },
      { model_id: 'qwen-image-plus-2026-01-09', capabilities: ['cheap', 'image_gen'],              rpm: 10,   cost: 0.0009 },
      { model_id: 'wan2.7-image',           capabilities: ['image_gen'],                       rpm: 10,   cost: 0.02 },
      { model_id: 'wan2.7-image-pro',       capabilities: ['cheap', 'image_gen'],              rpm: 10,   cost: 0.007 },
      { model_id: 'z-image-turbo',          capabilities: ['cheap', 'image_gen'],              rpm: 10,   cost: 0.0026 },
    ],
  },
  {
    key: 'gemini', brand: 'gemini',
    name: 'Google Gemini',
    base_url: 'https://generativelanguage.googleapis.com/v1beta/openai',
    balance_url: '',
    models: [
      { model_id: 'gemini-2.5-pro',        capabilities: ['text', 'vision', 'thinking'], rpm: 30,  cost: 0.005 },
      { model_id: 'gemini-2.5-flash',      capabilities: ['text', 'vision', 'cheap'],    rpm: 100, cost: 0.001 },
      { model_id: 'gemini-2.0-flash-lite', capabilities: ['text', 'cheap'],              rpm: 200, cost: 0.001 },
    ],
  },
  {
    key: 'openrouter', brand: 'openrouter',
    name: 'OpenRouter',
    base_url: 'https://openrouter.ai/api/v1',
    balance_url: 'https://openrouter.ai/api/v1/credits',
    models: [
      { model_id: 'anthropic/claude-opus-4.6',       capabilities: ['text', 'vision', 'thinking'], rpm: 30,  cost: 0.025 },
      { model_id: 'anthropic/claude-sonnet-4.6',      capabilities: ['text', 'vision', 'thinking'], rpm: 50,  cost: 0.015 },
      { model_id: 'openai/gpt-5.4',                   capabilities: ['text', 'vision', 'thinking'], rpm: 30,  cost: 0.015 },
      { model_id: 'google/gemini-2.5-pro',            capabilities: ['text', 'vision', 'thinking'], rpm: 30,  cost: 0.005 },
      { model_id: 'google/gemini-2.5-flash',          capabilities: ['text', 'vision', 'cheap'],    rpm: 100, cost: 0.001 },
      { model_id: 'deepseek/deepseek-chat',           capabilities: ['text'],                        rpm: 60,  cost: 0.001 },
      { model_id: 'deepseek/deepseek-r1',             capabilities: ['text', 'thinking'],            rpm: 30,  cost: 0.002 },
      { model_id: 'x-ai/grok-3',                      capabilities: ['text', 'thinking'],            rpm: 30,  cost: 0.010 },
      { model_id: 'openai/o3',                         capabilities: ['text', 'vision', 'thinking'], rpm: 30,  cost: 0.010 },
      { model_id: 'openai/gpt-5.4-mini',               capabilities: ['text', 'vision', 'cheap'],    rpm: 60,  cost: 0.005 },
    ],
  },
  {
    key: 'mimo', brand: 'mimo',
    name: 'Xiaomi MiMo',
    base_url: 'https://api.xiaomimimo.com/v1',
    balance_url: 'https://api.xiaomimimo.com/v1/dashboard/billing/subscription',
    models: [
      { model_id: 'MiMo-V2-Pro',    capabilities: ['text', 'vision', 'thinking'], rpm: 30,  cost: 0.003 },
      { model_id: 'MiMo-V2-Omni',   capabilities: ['text', 'vision'],             rpm: 60,  cost: 0.002 },
      { model_id: 'MiMo-V2-Flash',  capabilities: ['text', 'cheap'],              rpm: 200, cost: 0.001 },
    ],
  },
  // Meituan template loaded from static/provider_templates/meituan.json (if present)
  {
    key: 'yeysai', brand: 'tsinghua',
    name: 'YEYSAI (THUNLP / Tsinghua)',
    base_url: 'https://yeysai.com/v1',
    balance_url: 'https://yeysai.com/v1/dashboard/billing/subscription',
    models: [
      // ── Text models ──
      { model_id: 'claude-opus-4-6-thinking',       capabilities: ['text', 'vision', 'thinking'],   rpm: 30,  cost: 0.020 },
      { model_id: 'claude-opus-4-6',                 capabilities: ['text', 'vision'],               rpm: 30,  cost: 0.015 },
      { model_id: 'gpt-5.4',                         capabilities: ['text', 'vision'],               rpm: 60,  cost: 0.010 },
      { model_id: 'gpt-5.4-pro',                     capabilities: ['text', 'vision', 'thinking'],   rpm: 20,  cost: 0.020 },
      { model_id: 'gpt-5.4-mini',                    capabilities: ['text', 'vision', 'cheap'],      rpm: 120, cost: 0.003 },
      { model_id: 'gpt-5.4-nano',                    capabilities: ['text', 'cheap'],                rpm: 200, cost: 0.001 },
      { model_id: 'gemini-3.1-pro-preview-thinking',  capabilities: ['text', 'vision', 'thinking'],   rpm: 30,  cost: 0.005 },
      { model_id: 'gemini-3.1-flash-lite-preview',    capabilities: ['text', 'cheap'],                rpm: 200, cost: 0.001 },
      // ── Image generation ──
      { model_id: 'gemini-3-pro-image-preview',       capabilities: ['image_gen'],                    rpm: 10,  cost: 0.020 },
      { model_id: 'gemini-2.5-flash-image-preview',   capabilities: ['image_gen'],                    rpm: 10,  cost: 0.015 },
      // ── Embeddings ──
      { model_id: 'text-embedding-3-large',           capabilities: ['embedding'],                    rpm: 60,  cost: 0.001 },
      { model_id: 'text-embedding-3-small',           capabilities: ['embedding'],                    rpm: 60,  cost: 0.001 },
    ],
  },
];

/**
 * Load external provider templates from static/provider_templates/*.json.
 * The server exposes a listing endpoint; if unavailable, silently skip.
 * Templates are merged into _PROVIDER_TEMPLATES (avoiding duplicates by key).
 */
let _externalTemplatesLoaded = false;
async function _loadExternalProviderTemplates() {
  if (_externalTemplatesLoaded) return;
  _externalTemplatesLoaded = true;
  try {
    var r = await fetch(apiUrl('/api/provider-templates'));
    if (!r.ok) return;
    var templates = await r.json();
    if (!Array.isArray(templates)) return;
    var existingKeys = new Set(_PROVIDER_TEMPLATES.map(function(t) { return t.key; }));
    for (var i = 0; i < templates.length; i++) {
      var tpl = templates[i];
      if (tpl.key && !existingKeys.has(tpl.key)) {
        _PROVIDER_TEMPLATES.push(tpl);
        existingKeys.add(tpl.key);
        debugLog('[Settings] Loaded external provider template: ' + tpl.key, 'info');
      }
    }
  } catch (e) {
    // External templates are optional — silently skip
    debugLog('[Settings] External provider templates not available: ' + e.message, 'info');
  }
}

// ══════════════════════════════════════════════════════
//  Auto Setup — URL-first provider onboarding flow
// ══════════════════════════════════════════════════════

/**
 * Show the Auto Setup modal. User enters only Base URL + API Key,
 * the system probes the provider and creates a fully configured card.
 */
function _showAutoSetupModal() {
  // Remove any existing modal
  var existing = document.getElementById('stgAutoSetupModal');
  if (existing) existing.remove();

  var html = '<div id="stgAutoSetupModal" class="stg-modal-overlay" onclick="if(event.target===this)this.remove()">' +
    '<div class="stg-modal">' +
      '<div class="stg-modal-header">' +
        '<span class="stg-modal-title">🚀 自动配置服务商</span>' +
        '<button class="stg-modal-close" onclick="document.getElementById(\'stgAutoSetupModal\').remove()">✕</button>' +
      '</div>' +
      '<div class="stg-modal-body">' +
        '<p class="stg-modal-desc">只需填写 API 地址和密钥，系统将自动发现模型、检测余额接口、识别服务商品牌并获取定价信息。</p>' +
        '<div class="stg-field">' +
          '<label>API 地址 (Base URL) <span class="stg-required">*</span></label>' +
          '<input type="text" id="stgAutoUrl" placeholder="https://api.deepseek.com" autocomplete="url">' +
          '<span class="stg-hint">填写 OpenAI 兼容的 API 地址，通常以 /v1 结尾</span>' +
        '</div>' +
        '<div class="stg-field">' +
          '<label>API 密钥 <span class="stg-required">*</span></label>' +
          '<input type="password" id="stgAutoKey" placeholder="sk-..." autocomplete="off">' +
        '</div>' +
        '<div class="stg-field">' +
          '<label>模型发现路径 <span class="stg-hint">（可选 — 默认 /models）</span></label>' +
          '<input type="text" id="stgAutoModelsPath" placeholder="/models">' +
        '</div>' +
        '<div id="stgAutoStatus" class="stg-auto-status" style="display:none"></div>' +
      '</div>' +
      '<div class="stg-modal-footer">' +
        '<button class="stg-btn-secondary" onclick="document.getElementById(\'stgAutoSetupModal\').remove()">取消</button>' +
        '<button class="stg-btn-primary" id="stgAutoProbeBtn" onclick="_runAutoProbe()">🔍 开始探测</button>' +
      '</div>' +
    '</div>' +
  '</div>';

  document.body.insertAdjacentHTML('beforeend', html);
  // Focus the URL input
  setTimeout(function() {
    var urlInput = document.getElementById('stgAutoUrl');
    if (urlInput) urlInput.focus();
  }, 100);
}

/**
 * Run the auto-probe: call /api/provider-probe and create the provider.
 */
async function _runAutoProbe() {
  var baseUrl = (document.getElementById('stgAutoUrl').value || '').trim();
  var apiKey = (document.getElementById('stgAutoKey').value || '').trim();
  var modelsPath = (document.getElementById('stgAutoModelsPath').value || '').trim();
  var statusDiv = document.getElementById('stgAutoStatus');
  var probeBtn = document.getElementById('stgAutoProbeBtn');

  if (!baseUrl) {
    _showAutoStatus('error', '请填写 API 地址');
    return;
  }
  if (!apiKey) {
    _showAutoStatus('error', '请填写 API 密钥');
    return;
  }

  // Normalize URL: ensure scheme
  if (!baseUrl.startsWith('http://') && !baseUrl.startsWith('https://')) {
    baseUrl = 'https://' + baseUrl;
    document.getElementById('stgAutoUrl').value = baseUrl;
  }

  // Show progress
  if (probeBtn) {
    probeBtn.disabled = true;
    probeBtn.textContent = '⏳ 正在探测…';
  }
  _showAutoStatus('loading', '正在发现模型… 这可能需要几秒钟');

  try {
    var resp = await fetch(apiUrl('/api/provider-probe'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ base_url: baseUrl, api_key: apiKey, models_path: modelsPath || '' }),
    });

    var ct = (resp.headers.get('content-type') || '');
    if (ct.indexOf('application/json') < 0) {
      var txt = await resp.text();
      _showAutoStatus('error', '探测失败 (HTTP ' + resp.status + '): ' + txt.substring(0, 200));
      return;
    }

    var data = await resp.json();

    if (!data.ok) {
      _showAutoStatus('error', data.error || '探测失败');
      return;
    }

    // ── Success: create the provider ──
    var models = data.models || [];
    var summary = data.summary || {};

    // Build a summary message
    var parts = [];
    if (summary.text) parts.push(summary.text + ' 个文本');
    if (summary.thinking) parts.push(summary.thinking + ' 个推理');
    if (summary.vision) parts.push(summary.vision + ' 个视觉');
    if (summary.cheap) parts.push(summary.cheap + ' 个低价');
    if (summary.image_gen) parts.push(summary.image_gen + ' 个图片生成');
    if (summary.embedding) parts.push(summary.embedding + ' 个嵌入');
    var modelSummary = parts.join('，') || (models.length + ' 个模型');

    _showAutoStatus('success',
      '✅ 发现 ' + models.length + ' 个模型（' + modelSummary + '）' +
      (data.balance_url ? '，已检测到余额接口' : '') +
      (data.thinking_format ? '，建议思维格式: ' + data.thinking_format : ''));

    // Create the provider entry
    var provId = (data.brand || 'prov') + '_' + Date.now().toString(36);
    var newProv = {
      id: provId,
      name: data.name || 'Auto Provider',
      base_url: baseUrl,
      api_keys: [apiKey],
      enabled: true,
      models: models,
      brand: data.brand || 'generic',
      balance_url: data.balance_url || '',
    };
    if (data.thinking_format) {
      newProv.thinking_format = data.thinking_format;
    }

    _stgProviders.unshift(newProv);
    _renderProvidersTab();
    _renderPresetsTab(_serverConfig);

    // Close modal after a short delay so user sees the success message
    setTimeout(function() {
      var modal = document.getElementById('stgAutoSetupModal');
      if (modal) modal.remove();

      // Expand the new provider card and scroll to it
      var first = document.querySelector('.stg-provider-card');
      if (first) {
        first.classList.add('expanded');
        first.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }
    }, 1500);

  } catch (e) {
    _showAutoStatus('error', '网络错误: ' + e.message);
  } finally {
    if (probeBtn) {
      probeBtn.disabled = false;
      probeBtn.textContent = '🔍 开始探测';
    }
  }
}

/** Show a status message in the auto-setup modal */
function _showAutoStatus(type, msg) {
  var div = document.getElementById('stgAutoStatus');
  if (!div) return;
  div.style.display = 'block';
  div.className = 'stg-auto-status stg-auto-' + type;
  div.textContent = msg;
}

// ══════════════════════════════════════════════════════
//  Live state
// ══════════════════════════════════════════════════════

// providers[]: each has { id, name, base_url, api_keys:[], enabled, models:[], extra_headers:{} }
//   models[]: each has { model_id, aliases:[], capabilities:[], rpm, cost, thinking_default }
let _stgProviders = [];
let _stgPresets = {};  // kept for backward-compat save/load, but no longer used for preset→model mapping

// ══════════════════════════════════════════════════════
//  Helpers
// ══════════════════════════════════════════════════════

/** Collect all models from all providers (flat list with provider info) */
function _getAllModels() {
  var result = [];
  for (var pi = 0; pi < _stgProviders.length; pi++) {
    var p = _stgProviders[pi];
    var models = p.models || [];
    for (var mi = 0; mi < models.length; mi++) {
      result.push({ model: models[mi], provider: p, provIdx: pi, modelIdx: mi });
    }
  }
  return result;
}

function _setVal(id, value, prop) {
  var el = document.getElementById(id);
  if (!el) return;
  if (prop === 'checked') el.checked = !!value;
  else el.value = value;
}

// ══════════════════════════════════════════════════════
//  Tab switching & config loading
// ══════════════════════════════════════════════════════

function switchSettingsTab(tabId) {
  document.querySelectorAll('.settings-tab').forEach(function(t) {
    t.classList.toggle('active', t.dataset.tab === tabId);
  });
  document.querySelectorAll('.settings-tab-panel').forEach(function(p) {
    p.classList.toggle('active', p.id === 'settingsTab_' + tabId);
  });
}

async function _loadServerConfig() {
  try {
    var url = apiUrl('/api/server-config');
    debugLog('[Settings] Loading server config from ' + url, 'info');
    var r = await fetch(url);
    if (!r.ok) throw new Error('HTTP ' + r.status);
    _serverConfig = await r.json();
    debugLog('[Settings] Server config loaded: ' + (_serverConfig.providers || []).length + ' providers, ' + Object.keys(_serverConfig.presets || {}).length + ' presets', 'info');
    // Populate pricing cache so model cards can look up input/output costs
    if (_serverConfig.model_pricing && typeof _modelPricingCache !== 'undefined') {
      _modelPricingCache = _serverConfig.model_pricing;
    }
    return _serverConfig;
  } catch (e) {
    debugLog('[Settings] Failed to load server config: ' + e.message, 'error');
    return null;
  }
}

function openSettings() {
  // ── General tab: populate from local config ──
  document.getElementById("settingTemp").value = config.temperature;
  document.getElementById("tempVal").textContent = config.temperature;
  document.getElementById("settingMaxTokens").value = config.maxTokens;
  document.getElementById("settingImageMaxWidth").value = config.imageMaxWidth || 1024;
  document.getElementById("settingSystem").value = config.systemPrompt || "";

  // Default thinking depth
  var dtd = document.getElementById('settingDefaultThinkingDepth');
  if (dtd) dtd.value = config.defaultThinkingDepth || 'off';

  // Trading module toggle
  var tradingCb = document.getElementById('settingTradingEnabled');
  if (tradingCb) {
    tradingCb.checked = !!(typeof _featureFlags !== 'undefined' && _featureFlags.trading_enabled);
    tradingCb.onchange = function() {
      document.getElementById('tradingRestartHint').style.display =
        (this.checked !== !!(typeof _featureFlags !== 'undefined' && _featureFlags.trading_enabled)) ? 'block' : 'none';
    };
  }

  // Theme picker sync
  var ct = _getCurrentTheme();
  document.querySelectorAll(".theme-option").forEach(function(el) {
    el.classList.toggle("active", el.dataset.theme === ct);
  });

  switchSettingsTab('general');
  document.getElementById("settingsModal").classList.add("open");
  document.getElementById('settingsStatusHint').textContent = '';

  // Load OAuth status
  _loadOAuthStatus();

  // Show version in footer
  var verEl = document.getElementById('settingsVersion');
  if (verEl) {
    fetch('api/health').then(function(r){return r.json()}).then(function(d){
      if(d.version) verEl.textContent = 'v' + d.version;
    }).catch(function(){});
  }

  // Show loading states
  var provList = document.getElementById('stgProviderList');
  if (provList) provList.innerHTML = '<p class="stg-loading">正在加载配置…</p>';
  var presetTable = document.getElementById('stgPresetTable');
  if (presetTable) presetTable.innerHTML = '<p class="stg-loading">正在加载…</p>';

  // ── Load server config for other tabs ──
  _loadServerConfig().then(function(cfg) {
    if (!cfg) {
      document.getElementById('settingsStatusHint').textContent = '⚠️ 无法加载服务器配置';
      if (provList) provList.innerHTML = '<p class="stg-empty">加载服务器配置失败。请检查服务器是否正在运行。</p>';
      if (presetTable) presetTable.innerHTML = '<p class="stg-empty">加载模型预设失败。</p>';
      debugLog('[Settings] Config load failed — provider list and preset table set to error state', 'warning');
      return;
    }
    // Deep-copy providers (they include nested models now)
    _stgProviders = JSON.parse(JSON.stringify(cfg.providers || []));
    _stgPresets = JSON.parse(JSON.stringify(cfg.presets || {}));

    // Pre-load external templates so sync buttons appear on first render
    _loadExternalProviderTemplates().finally(function() {
      _renderProvidersTab();
      // Start auto-polling balance for all eligible providers
      _startBalancePolling();
    });
    _renderPresetsTab(cfg);
    _populateSearchTab(cfg);
    _populateNetworkTab(cfg);
    _populateAdvancedTab(cfg);
    _populateFeishuTab(cfg);
  });
}

// ══════════════════════════════════════════════════════
//  Providers Tab — Provider CRUD + nested model list
// ══════════════════════════════════════════════════════

function _renderProvidersTab() {
  var list = document.getElementById('stgProviderList');
  if (!list) return;

  if (_stgProviders.length === 0) {
    list.innerHTML = '<p class="stg-empty">还没有配置服务商。点击“+ 自定义服务商”开始添加。</p>';
    return;
  }

  var html = '';
  for (var pi = 0; pi < _stgProviders.length; pi++) {
    var p = _stgProviders[pi];
    var models = p.models || [];
    var keyCount = (p.api_keys || []).length;
    // Use explicit brand if stored (from template), else detect from hints
    var brand = p.brand || _detectBrand(p.name + ' ' + (p.base_url || ''));

    html += '<div class="stg-provider-card' + (pi === 0 ? ' expanded' : '') + '" data-prov-idx="' + pi + '">';

    // ── Header ──
    html += '<div class="stg-provider-head" onclick="_toggleProviderExpand(this.parentElement)">' +
      '<div class="stg-provider-icon">' + _brandSvg(brand, 22) + '</div>' +
      '<div class="stg-provider-info">' +
        '<div class="stg-provider-name">' + escapeHtml(p.name || 'Unnamed') + '</div>' +
        '<div class="stg-provider-url">' + escapeHtml(p.base_url || '—') + '</div>' +
      '</div>' +
      '<div class="stg-provider-badges">' +
        '<span class="stg-badge">' + keyCount + ' 个密钥</span>' +
        '<span class="stg-badge">' + models.length + ' 个模型</span>' +
        (p.enabled === false ? '<span class="stg-badge off">已禁用</span>' : '') +
      '</div>' +
      '<span class="stg-chevron">▾</span>' +
    '</div>';

    // ── Expanded body ──
    html += '<div class="stg-provider-body">';

    // Provider fields
    html += '<div class="stg-field-grid">' +
      '<div class="stg-field"><label>显示名称</label>' +
        '<input type="text" value="' + escapeHtml(p.name || '') + '" onchange="_onProvField(' + pi + ',\'name\',this.value)"></div>' +
      '<div class="stg-field"><label>API 地址 (Base URL)</label>' +
        '<input type="text" value="' + escapeHtml(p.base_url || '') + '" placeholder="https://api.openai.com/v1" onchange="_onProvField(' + pi + ',\'base_url\',this.value)"></div>' +
    '</div>';

    html += '<div class="stg-field"><label>API 密钥 <span class="stg-hint">（每行一个，安全存储）</span></label>' +
      '<textarea rows="' + Math.max(2, Math.min(5, keyCount)) + '" onchange="_onProvKeys(' + pi + ',this.value)">' + escapeHtml((p.api_keys || []).join('\n')) + '</textarea></div>';

    // ── Balance URL field + Check Balance button ──
    var balancePlaceholder = (p.base_url && _guessBalanceUrl(p.base_url))
      ? escapeHtml(_guessBalanceUrl(p.base_url))
      : 'https://api.example.com/v1/dashboard/billing/subscription';
    html += '<div class="stg-field"><label>余额查询地址 <span class="stg-hint">（可选 — OpenAI 兼容的账单接口）</span></label>' +
      '<div class="stg-balance-row">' +
        '<input type="text" value="' + escapeHtml(p.balance_url || '') + '" placeholder="' + balancePlaceholder + '" onchange="_onProvField(' + pi + ',\'balance_url\',this.value)">' +
        '<button class="stg-btn-balance" onclick="_checkProviderBalance(' + pi + ')" title="查询余额">查询 ▸</button>' +
      '</div>' +
      '<div class="stg-balance-result" id="stgBalanceResult_' + pi + '"></div>' +
    '</div>';

    // ── Models Discovery Path (optional, for non-standard /v1/models paths) ──
    var modelsPlaceholder = p.base_url ? escapeHtml(p.base_url.replace(/\/+$/, '') + '/models') : '/models';
    html += '<div class="stg-field"><label>模型发现路径 <span class="stg-hint">（可选 — 默认在 Base URL 后追加 /models）</span></label>' +
      '<input type="text" value="' + escapeHtml(p.models_path || '') + '" placeholder="' + modelsPlaceholder + '" onchange="_onProvField(' + pi + ',\'models_path\',this.value)"></div>';

    // ── Extra Headers (optional, for provider-specific gateway headers) ──
    var extraHdrsJson = (p.extra_headers && Object.keys(p.extra_headers).length > 0)
      ? JSON.stringify(p.extra_headers, null, 2) : '';
    html += '<div class="stg-field"><label>自定义请求头 <span class="stg-hint">（可选 — JSON 对象，如 {"X-My-Header": "value"}）</span></label>' +
      '<textarea rows="2" placeholder=\'{"Header-Name": "value"}\' onchange="_onProvExtraHeaders(' + pi + ',this.value)">' + escapeHtml(extraHdrsJson) + '</textarea></div>';

    // ── Thinking Format (per-provider thinking parameter style) ──
    var tfVal = p.thinking_format || '';
    html += '<div class="stg-field"><label>思维参数格式 <span class="stg-hint">（默认自动检测 — 仅当端点使用非标准格式时需配置）</span></label>' +
      '<select onchange="_onProvField(' + pi + ',\'thinking_format\',this.value)">' +
        '<option value=""'  + (tfVal === '' ? ' selected' : '') + '>自动检测（按模型名称）</option>' +
        '<option value="enable_thinking"' + (tfVal === 'enable_thinking' ? ' selected' : '') + '>enable_thinking（LongCat/Qwen/Gemini 风格）</option>' +
        '<option value="thinking_type"' + (tfVal === 'thinking_type' ? ' selected' : '') + '>thinking.type（Doubao/Claude 风格）</option>' +
        '<option value="none"' + (tfVal === 'none' ? ' selected' : '') + '>不发送思维参数</option>' +
      '</select></div>';

    html += '<div class="stg-field-row">' +
      '<div class="stg-toggle-row"><span>启用</span>' +
        '<label class="stg-toggle"><input type="checkbox"' + (p.enabled !== false ? ' checked' : '') + ' onchange="_onProvField(' + pi + ',\'enabled\',this.checked)">' +
        '<span class="stg-toggle-track"><span class="stg-toggle-thumb"></span></span></label>' +
      '</div>' +
      '<button class="stg-btn-danger" onclick="_deleteProvider(' + pi + ')">🗑 删除服务商</button>' +
    '</div>';

    // ── Nested Model List ──
    html += '<div class="stg-models-section">' +
      '<div class="stg-models-header">' +
        '<span class="stg-models-title">模型列表</span>' +
        '<div class="stg-models-actions">' +
          '<button class="stg-btn-add" onclick="_discoverModels(' + pi + ')" title="从 /v1/models 接口自动发现模型">🔍 自动发现</button>' +
          '<button class="stg-btn-add" onclick="_addModel(' + pi + ')">+ 添加模型</button>' +
        '</div>' +
      '</div>';

    if (models.length === 0) {
      html += '<p class="stg-empty-sm">还没有配置模型。点击“🔍 自动发现”自动检测可用模型，或点击“+ 添加模型”手动添加。</p>';
    } else {
      html += '<div class="stg-model-list">';
      for (var mi = 0; mi < models.length; mi++) {
        html += _renderModelCard(pi, mi, models[mi]);
      }
      html += '</div>';
    }
    html += '</div>'; // /stg-models-section

    html += '</div>'; // /stg-provider-body
    html += '</div>'; // /stg-provider-card
  }
  list.innerHTML = html;
}

/** Format a $/1M-tokens price for compact display */
function _fmtPrice(val) {
  if (val === 0 || val === '0') return '免费';
  if (val == null) return '—';
  var n = parseFloat(val);
  if (isNaN(n)) return '—';
  // ≥1: show up to 2 decimals;  <1: show up to 3 significant digits
  var s;
  if (n >= 1) {
    s = n.toFixed(2).replace(/\.?0+$/, '');
  } else {
    s = n.toPrecision(3).replace(/\.?0+$/, '');
    // toPrecision can return scientific notation for very small numbers
    if (s.indexOf('e') >= 0) s = n.toFixed(4).replace(/\.?0+$/, '');
  }
  return '$' + s;
}

function _renderModelCard(provIdx, modelIdx, m) {
  var brand = _detectBrand(m.model_id);
  var caps = m.capabilities || [];
  var aliases = m.aliases || [];

  var html = '<div class="stg-mcard" data-prov="' + provIdx + '" data-model="' + modelIdx + '">';

  // Brand icon
  html += '<div class="stg-mcard-icon">' + _brandSvg(brand, 18) + '</div>';

  // Body
  html += '<div class="stg-mcard-body">';

  // Model ID line
  html += '<div class="stg-mcard-main">' +
    '<span class="stg-mcard-id">' + escapeHtml(m.model_id || '(unnamed)') + '</span>';

  html += '</div>';

  // Capabilities + RPM
  if (caps.length > 0) {
    html += '<div class="stg-mcard-caps">';
    for (var ci = 0; ci < caps.length; ci++) {
      html += '<span class="stg-cap ' + caps[ci] + '">' + escapeHtml(caps[ci]) + '</span>';
    }
    if (m.rpm) html += '<span class="stg-mcard-stat">⏱ ' + m.rpm + ' rpm</span>';
    html += '</div>';
  }

  // Pricing row — look up real input/output from pricing cache
  var mp = (typeof _modelPricingCache !== 'undefined' && _modelPricingCache) ? _modelPricingCache[m.model_id] : null;
  if (mp && (mp.input != null || mp.output != null)) {
    var isFree = (mp.input === 0 && mp.output === 0);
    if (isFree) {
      html += '<div class="stg-mcard-pricing"><span class="stg-price-free">免费</span></div>';
    } else {
      html += '<div class="stg-mcard-pricing">' +
        '<span class="stg-price-label">输入</span>' +
        '<span class="stg-price-val in">' + _fmtPrice(mp.input) + '</span>' +
        '<span class="stg-price-sep">/</span>' +
        '<span class="stg-price-label">输出</span>' +
        '<span class="stg-price-val out">' + _fmtPrice(mp.output) + '</span>' +
        '<span class="stg-price-unit">每百万 Token</span>' +
      '</div>';
    }
  } else {
    html += '<div class="stg-mcard-pricing"><span class="stg-price-na">暂无价格数据</span></div>';
  }

  // Aliases
  html += '<div class="stg-mcard-aliases">';
  if (aliases.length > 0) {
    html += '<span class="stg-aliases-label">别名：</span>';
    for (var ai = 0; ai < aliases.length; ai++) {
      html += '<span class="stg-alias-chip">' +
        escapeHtml(aliases[ai]) +
        '<span class="stg-alias-x" onclick="event.stopPropagation();_removeAlias(' + provIdx + ',' + modelIdx + ',' + ai + ')">×</span>' +
      '</span>';
    }
  }
  html += '<button class="stg-alias-add" onclick="event.stopPropagation();_addAlias(' + provIdx + ',' + modelIdx + ')">+ 别名</button>';
  html += '</div>';

  html += '</div>'; // /stg-mcard-body

  // Actions
  html += '<div class="stg-mcard-actions">' +
    '<button class="stg-btn-icon" onclick="_editModel(' + provIdx + ',' + modelIdx + ')" title="编辑">✎</button>' +
    '<button class="stg-btn-icon danger" onclick="_deleteModel(' + provIdx + ',' + modelIdx + ')" title="删除">✕</button>' +
  '</div>';

  html += '</div>'; // /stg-mcard
  return html;
}

// ── Provider CRUD ──

// ── URL Guess Helpers — generate best-guess balance/models URLs from base_url ──

/**
 * Known provider-specific balance URL patterns.
 * Key: substring to match in base_url (lowercase).
 * Value: function(baseUrl) → full balance URL.
 */
var _BALANCE_URL_RULES = [
  // DeepSeek uses a non-standard /user/balance endpoint
  { match: 'deepseek.com',   fn: function(b) { return _urlOrigin(b) + '/user/balance'; } },
  // OpenRouter uses /api/v1/credits
  { match: 'openrouter.ai',  fn: function(b) { return _urlOrigin(b) + '/api/v1/credits'; } },
  // Google Gemini has no billing API
  { match: 'googleapis.com', fn: function() { return ''; } },
];

/** Extract origin (scheme + host) from a URL string. */
function _urlOrigin(url) {
  try {
    var u = new URL(url);
    return u.origin;
  } catch (e) {
    return url.replace(/\/+$/, '');
  }
}

/**
 * Guess the balance/billing URL from a base_url.
 * Uses known provider rules first, then falls back to
 * base_url + '/dashboard/billing/subscription'.
 */
function _guessBalanceUrl(baseUrl) {
  if (!baseUrl) return '';
  var lower = baseUrl.toLowerCase();
  for (var i = 0; i < _BALANCE_URL_RULES.length; i++) {
    if (lower.indexOf(_BALANCE_URL_RULES[i].match) >= 0) {
      return _BALANCE_URL_RULES[i].fn(baseUrl);
    }
  }
  // Default: append /dashboard/billing/subscription to base_url
  return baseUrl.replace(/\/+$/, '') + '/dashboard/billing/subscription';
}

/**
 * Guess the models discovery path from a base_url.
 * Most OpenAI-compatible providers use /models (appended to base_url).
 * Returns empty string to use the default behavior.
 */
function _guessModelsPath(baseUrl) {
  // Default /models works for almost all providers — return empty
  // to let the backend use its default logic.
  return '';
}

function _toggleProviderExpand(card) {
  card.classList.toggle('expanded');
}

function _onProvField(provIdx, field, value) {
  if (!_stgProviders[provIdx]) return;
  _stgProviders[provIdx][field] = value;

  // When base_url changes, auto-fill balance_url and models_path if empty
  if (field === 'base_url' && value) {
    var p = _stgProviders[provIdx];
    if (!p.balance_url) {
      p.balance_url = _guessBalanceUrl(value);
    }
    if (!p.models_path) {
      p.models_path = _guessModelsPath(value);
    }
  }

  // Re-render header to reflect name/badge changes
  _renderProvidersTab();
}

function _onProvExtraHeaders(provIdx, value) {
  if (!_stgProviders[provIdx]) return;
  var trimmed = value.trim();
  if (!trimmed) {
    delete _stgProviders[provIdx].extra_headers;
    return;
  }
  try {
    var parsed = JSON.parse(trimmed);
    if (typeof parsed === 'object' && !Array.isArray(parsed)) {
      _stgProviders[provIdx].extra_headers = parsed;
    } else {
      debugLog('[Settings] Custom Headers must be a JSON object', 'error');
    }
  } catch (e) {
    debugLog('[Settings] Invalid JSON for Custom Headers: ' + e.message, 'error');
  }
}

function _onProvKeys(provIdx, value) {
  if (!_stgProviders[provIdx]) return;
  _stgProviders[provIdx].api_keys = value.split('\n').map(function(s) { return s.trim(); }).filter(Boolean);
}

/**
 * Check the balance/billing for a provider via its balance_url.
 * Calls the backend proxy endpoint which handles auth + network.
 */
async function _checkProviderBalance(provIdx) {
  var p = _stgProviders[provIdx];
  if (!p) return;
  var resultDiv = document.getElementById('stgBalanceResult_' + provIdx);
  if (!resultDiv) return;

  // Use explicit balance_url, or guess from base_url
  var balanceUrl = p.balance_url || _guessBalanceUrl(p.base_url || '');
  if (!balanceUrl) {
    resultDiv.innerHTML = '<span class="stg-balance-err">未配置余额查询地址</span>';
    return;
  }
  if (!p.api_keys || p.api_keys.length === 0) {
    resultDiv.innerHTML = '<span class="stg-balance-err">未配置 API 密钥</span>';
    return;
  }

  resultDiv.innerHTML = '<span class="stg-balance-loading">⏳ 查询中…</span>';

  try {
    var r = await fetch(apiUrl('/api/provider-balance'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        balance_url: balanceUrl,
        api_key: p.api_keys[0],
      }),
    });
    var data = await r.json();
    if (!data.ok) {
      resultDiv.innerHTML = '<span class="stg-balance-err">❌ ' + escapeHtml(data.error || '未知错误') + '</span>';
      return;
    }

    // Render balance info using unified format
    var info = data.balance;
    resultDiv.innerHTML = _renderBalanceInfo(info);
    // Cache balance for badge display
    _balanceCache[provIdx] = { info: info, ts: Date.now() };
    _updateBalanceBadge(provIdx, info);
    // If we used a guessed URL and it worked, persist it to the provider
    if (!p.balance_url && balanceUrl) {
      p.balance_url = balanceUrl;
      _renderProvidersTab();
    }
  } catch (e) {
    resultDiv.innerHTML = '<span class="stg-balance-err">❌ 网络错误: ' + escapeHtml(e.message) + '</span>';
  }
}

/**
 * Render balance info HTML from the unified backend format.
 * Handles: OpenAI (limit+used), DeepSeek (balance_infos), OpenRouter (credits), generic, raw.
 */
function _renderBalanceInfo(info) {
  var html = '<div class="stg-balance-info">';

  if (info.limit_usd != null && info.used_usd != null) {
    // ── Format with limit + used (OpenAI, OpenRouter) ──
    var used = info.used_usd;
    var limit = info.limit_usd;
    var remaining = info.balance_usd != null ? info.balance_usd : (limit - used);
    var pct = limit > 0 ? Math.round((used / limit) * 100) : 0;
    var barColor = pct > 90 ? '#ef4444' : pct > 70 ? '#f59e0b' : '#22c55e';
    html += '<div class="stg-balance-bar-wrap">' +
      '<div class="stg-balance-bar" style="width:' + Math.min(pct, 100) + '%;background:' + barColor + '"></div>' +
    '</div>';
    html += '<div class="stg-balance-nums">' +
      '<span>已用: <b>$' + used.toFixed(2) + '</b></span>' +
      '<span>剩余: <b>$' + remaining.toFixed(2) + '</b></span>' +
      '<span>额度: <b>$' + limit.toFixed(2) + '</b></span>' +
    '</div>';
  } else if (info.balance_usd != null) {
    // ── Balance-only format (DeepSeek, generic) ──
    var bal = info.balance_usd;
    var barColor = bal > 10 ? '#22c55e' : bal > 2 ? '#f59e0b' : '#ef4444';
    html += '<div class="stg-balance-nums">';
    html += '<span>余额: <b style="color:' + barColor + '">$' + bal.toFixed(2) + '</b></span>';
    if (info.currency && info.currency !== 'USD' && info.balance_local != null) {
      html += '<span>（' + info.currency + ' ' + info.balance_local.toFixed(2) + '）</span>';
    }
    if (info.granted_balance != null) {
      html += '<span>赠送: ' + info.currency + ' ' + info.granted_balance.toFixed(2) + '</span>';
    }
    if (info.is_available === false) {
      html += '<span style="color:#ef4444;font-weight:800">⚠ 余额不足</span>';
    }
    html += '</div>';
  } else if (info.raw) {
    // ── Raw fallback ──
    html += '<span class="stg-balance-raw">' + escapeHtml(JSON.stringify(info.raw)) + '</span>';
  } else {
    html += '<span class="stg-balance-raw">' + escapeHtml(JSON.stringify(info)) + '</span>';
  }

  html += '</div>';
  return html;
}

/**
 * Format a balance value for compact badge display.
 */
function _fmtBalanceBadge(info) {
  var b = null;
  if (info.balance_usd != null) {
    b = info.balance_usd;
  } else if (info.limit_usd != null && info.used_usd != null) {
    b = info.limit_usd - info.used_usd;
  }
  if (b == null) return null;
  if (b >= 1000) return '$' + (b / 1000).toFixed(1) + 'k';
  if (b >= 100) return '$' + Math.round(b);
  return '$' + b.toFixed(2);
}

/**
 * Update the balance badge in the provider header card.
 */
function _updateBalanceBadge(provIdx, info) {
  var card = document.querySelector('.stg-provider-card[data-prov-idx="' + provIdx + '"]');
  if (!card) return;
  var badges = card.querySelector('.stg-provider-badges');
  if (!badges) return;

  // Remove existing balance badge
  var existing = badges.querySelector('.stg-badge-balance');
  if (existing) existing.remove();

  var text = _fmtBalanceBadge(info);
  if (!text) return;

  var bal = info.balance_usd != null ? info.balance_usd :
            (info.limit_usd != null ? info.limit_usd - (info.used_usd || 0) : null);
  var colorClass = bal != null ? (bal > 10 ? 'ok' : bal > 2 ? 'warn' : 'low') : 'ok';

  var span = document.createElement('span');
  span.className = 'stg-badge stg-badge-balance stg-badge-bal-' + colorClass;
  span.textContent = '\uD83D\uDCB0 ' + text;
  span.title = '余额（点击刷新）';
  span.style.cursor = 'pointer';
  span.onclick = function(e) {
    e.stopPropagation();
    _checkProviderBalance(provIdx);
  };
  badges.appendChild(span);
}

// ── Balance auto-polling ──
var _balanceCache = {};  // provIdx → { info, ts }
var _balancePollTimer = null;
var _BALANCE_POLL_INTERVAL = 3 * 60 * 1000;  // 3 minutes

/**
 * Start auto-polling balance for all providers that have balance_url and api_keys.
 * Called when settings panel opens.
 */
function _startBalancePolling() {
  _stopBalancePolling();
  // Immediate first check for all eligible providers
  _pollAllBalances();
  _balancePollTimer = setInterval(_pollAllBalances, _BALANCE_POLL_INTERVAL);
}

function _stopBalancePolling() {
  if (_balancePollTimer) {
    clearInterval(_balancePollTimer);
    _balancePollTimer = null;
  }
}

async function _pollAllBalances() {
  for (var pi = 0; pi < _stgProviders.length; pi++) {
    var p = _stgProviders[pi];
    if (!p.balance_url || !p.api_keys || p.api_keys.length === 0) continue;
    if (p.enabled === false) continue;

    // Skip if recently checked (within 2 minutes)
    var cached = _balanceCache[pi];
    if (cached && (Date.now() - cached.ts) < 120000) {
      _updateBalanceBadge(pi, cached.info);
      continue;
    }

    // Fire balance check (don't await all — stagger slightly)
    (function(idx) {
      setTimeout(function() { _checkProviderBalanceSilent(idx); }, idx * 500);
    })(pi);
  }
}

/**
 * Silent balance check — updates badge without touching the result div.
 * Used by auto-polling.
 */
async function _checkProviderBalanceSilent(provIdx) {
  var p = _stgProviders[provIdx];
  if (!p || !p.balance_url || !p.api_keys || p.api_keys.length === 0) return;

  try {
    var r = await fetch(apiUrl('/api/provider-balance'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        balance_url: p.balance_url,
        api_key: p.api_keys[0],
      }),
    });
    var data = await r.json();
    if (!data.ok) return;

    var info = data.balance;
    _balanceCache[provIdx] = { info: info, ts: Date.now() };
    _updateBalanceBadge(provIdx, info);

    // Also update the detail result div if it exists and is visible
    var resultDiv = document.getElementById('stgBalanceResult_' + provIdx);
    if (resultDiv && resultDiv.offsetParent !== null) {
      resultDiv.innerHTML = _renderBalanceInfo(info);
    }
  } catch (e) {
    // Silent — don't bother the user with polling errors
    debugLog('[Balance] Silent poll failed for provider ' + provIdx + ': ' + e.message, 'debug');
  }
}

function _deleteProvider(provIdx) {
  var p = _stgProviders[provIdx];
  if (!p) return;
  if (!confirm('确定删除服务商“' + (p.name || p.id) + '”及其 ' + (p.models || []).length + ' 个模型吗？')) return;
  _stgProviders.splice(provIdx, 1);
  _renderProvidersTab();
  _renderPresetsTab(_serverConfig);
}

function addProvider() {
  var id = 'prov_' + Date.now().toString(36);
  _stgProviders.unshift({
    id: id, name: '新服务商', base_url: '', api_keys: [], enabled: true, models: []
  });
  _renderProvidersTab();
  // Expand the new provider (now first card)
  var first = document.querySelector('.stg-provider-card');
  if (first) {
    first.classList.add('expanded');
    first.scrollIntoView({ behavior: 'smooth', block: 'start' });
    var nameInput = first.querySelector('input');
    if (nameInput) { nameInput.select(); nameInput.focus(); }
  }
}

/**
 * Show the provider template dropdown menu anchored to the button.
 * Clicking a template calls addProviderFromTemplate(key).
 */
async function _showTemplateMenu(btn) {
  // Ensure external templates are loaded before showing menu
  await _loadExternalProviderTemplates();

  // Remove any existing menu
  var existing = document.getElementById('stgTemplateMenu');
  if (existing) { existing.remove(); return; }

  var menu = document.createElement('div');
  menu.id = 'stgTemplateMenu';
  menu.className = 'stg-template-menu';

  for (var i = 0; i < _PROVIDER_TEMPLATES.length; i++) {
    var tpl = _PROVIDER_TEMPLATES[i];
    var item = document.createElement('div');
    item.className = 'stg-template-item';
    item.setAttribute('data-tpl-key', tpl.key);
    item.innerHTML = _brandSvg(tpl.brand, 18) +
      '<span class="stg-template-name">' + escapeHtml(tpl.name) + '</span>' +
      '<span class="stg-template-models">' + tpl.models.length + ' 个模型</span>';
    item.onclick = (function(key) {
      return function() {
        addProviderFromTemplate(key);
        menu.remove();
      };
    })(tpl.key);
    menu.appendChild(item);
  }

  // Position below button
  btn.parentElement.style.position = 'relative';
  btn.parentElement.appendChild(menu);

  // Close on outside click
  setTimeout(function() {
    document.addEventListener('click', function _closeMenu(e) {
      if (!menu.contains(e.target) && e.target !== btn) {
        menu.remove();
        document.removeEventListener('click', _closeMenu);
      }
    });
  }, 0);
}

/**
 * Add a pre-configured provider from a template.
 * Pre-fills base_url and models; user just needs to add their API key.
 */
function addProviderFromTemplate(templateKey) {
  var tpl = null;
  for (var i = 0; i < _PROVIDER_TEMPLATES.length; i++) {
    if (_PROVIDER_TEMPLATES[i].key === templateKey) { tpl = _PROVIDER_TEMPLATES[i]; break; }
  }
  if (!tpl) return;

  // Check if this provider is already added
  for (var j = 0; j < _stgProviders.length; j++) {
    if (_stgProviders[j].base_url === tpl.base_url) {
      if (!confirm(tpl.name + '（相同 API 地址）似乎已添加。要再添加一个吗？')) return;
      break;
    }
  }

  var id = tpl.key + '_' + Date.now().toString(36);
  var models = tpl.models.map(function(m) {
    return {
      model_id: m.model_id,
      aliases: m.aliases || [],
      capabilities: m.capabilities || ['text'],
      rpm: m.rpm || 30,
      cost: m.cost || 0.01,
      thinking_default: (m.capabilities || []).indexOf('thinking') >= 0,
    };
  });

  var newProv = {
    id: id, name: tpl.name, base_url: tpl.base_url,
    balance_url: tpl.balance_url || '',
    brand: tpl.brand || '',
    api_keys: [], enabled: true, models: models,
  };
  if (tpl.extra_headers && Object.keys(tpl.extra_headers).length > 0) {
    newProv.extra_headers = JSON.parse(JSON.stringify(tpl.extra_headers));
  }
  if (tpl.thinking_format) {
    newProv.thinking_format = tpl.thinking_format;
  }
  _stgProviders.unshift(newProv);
  _renderProvidersTab();
  _renderPresetsTab(_serverConfig);

  // Expand the new provider (now first card) and focus the API key textarea
  var first = document.querySelector('.stg-provider-card');
  if (first) {
    first.classList.add('expanded');
    first.scrollIntoView({ behavior: 'smooth', block: 'start' });
    var textarea = first.querySelector('textarea');
    if (textarea) {
      textarea.focus();
      textarea.placeholder = '在此粘贴你的 ' + tpl.name + ' API 密钥';
    }
  }
}

// ── Template Sync: merge new models from matching template into existing provider ──

/**
 * Find the matching template for a provider by base_url or brand+key.
 * Returns the template object, or null if no match.
 */
function _findMatchingTemplate(provider) {
  if (!provider) return null;
  var url = (provider.base_url || '').replace(/\/+$/, '');
  // 1. Exact base_url match
  for (var i = 0; i < _PROVIDER_TEMPLATES.length; i++) {
    var tUrl = (_PROVIDER_TEMPLATES[i].base_url || '').replace(/\/+$/, '');
    if (tUrl && url && tUrl === url) return _PROVIDER_TEMPLATES[i];
  }
  // 2. Fallback: match by brand (if explicitly set from a previous template apply)
  if (provider.brand) {
    for (var j = 0; j < _PROVIDER_TEMPLATES.length; j++) {
      if (_PROVIDER_TEMPLATES[j].brand === provider.brand || _PROVIDER_TEMPLATES[j].key === provider.brand) {
        return _PROVIDER_TEMPLATES[j];
      }
    }
  }
  return null;
}

// ── Model Auto-Discovery ──

async function _discoverModels(provIdx) {
  var p = _stgProviders[provIdx];
  if (!p) return;

  var baseUrl = (p.base_url || '').trim();
  var apiKey = (p.api_keys && p.api_keys[0]) || '';

  if (!baseUrl) {
    alert('请先设置 API 地址 (Base URL) 再进行模型发现。');
    return;
  }
  if (!apiKey) {
    alert('请先添加至少一个 API 密钥再进行模型发现。');
    return;
  }

  var modelsPath = (p.models_path || '').trim();

  // Find the discover button and show loading state
  var cards = document.querySelectorAll('.stg-provider-card');
  var card = cards[provIdx];
  var btn = card ? card.querySelector('button[onclick*="_discoverModels"]') : null;
  var oldText = btn ? btn.textContent : '';
  if (btn) {
    btn.disabled = true;
    btn.textContent = '⏳ 发现中…';
  }

  try {
    var resp = await fetch(apiUrl('/api/discover-models'), {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ base_url: baseUrl, api_key: apiKey, models_path: modelsPath || '' })
    });

    // Guard: response may be non-JSON (proxy 415, HTML error page, etc.)
    var ct = (resp.headers.get('content-type') || '');
    if (ct.indexOf('application/json') < 0) {
      var txt = await resp.text();
      alert('发现失败 (HTTP ' + resp.status + '): ' + txt.substring(0, 200));
      return;
    }
    var data = await resp.json();

    if (!data.ok) {
      alert('发现失败: ' + (data.error || '未知错误'));
      return;
    }

    var discovered = data.models || [];
    if (discovered.length === 0) {
      alert('在 ' + baseUrl + ' 未找到模型');
      return;
    }

    // Merge: add only models not already present
    if (!p.models) p.models = [];
    var existing = new Set(p.models.map(function(m) { return m.model_id; }));
    var added = 0;
    for (var i = 0; i < discovered.length; i++) {
      if (!existing.has(discovered[i].model_id)) {
        p.models.push(discovered[i]);
        existing.add(discovered[i].model_id);
        added++;
      }
    }

    _renderProvidersTab();
    // Expand the provider to show results
    var newCards = document.querySelectorAll('.stg-provider-card');
    if (newCards[provIdx]) newCards[provIdx].classList.add('expanded');

    var nCheap = discovered.filter(function(m) { return (m.capabilities || []).indexOf('cheap') >= 0; }).length;
    var msg = '✅ 发现 ' + discovered.length + ' 个模型（' + nCheap + ' 个标记为低价）。\n' +
              '新增 ' + added + ' 个模型' + (added < discovered.length ? '，' + (discovered.length - added) + ' 个已存在。' : '。');
    alert(msg);

    // Offer to persist discovered models into the hardcoded template
    var tpl = _findMatchingTemplate(p);
    if (tpl && added > 0) {
      _offerTemplateUpdate(tpl.key, p.models);
    }

  } catch (e) {
    alert('发现出错: ' + e.message);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = oldText;
    }
  }
}

// ── Persist Discovered Models into Hardcoded Template ──

/**
 * Offer to update the hardcoded provider template with the current model list.
 * Called after discovery finds new models for a template-matched provider.
 */
async function _offerTemplateUpdate(templateKey, models) {
  var ok = confirm(
    '是否将当前模型列表（' + models.length + ' 个）写入源码模板？\n\n' +
    '这样新部署时就自带最新模型，无需再次发现。'
  );
  if (!ok) return;

  try {
    var resp = await fetch(apiUrl('/api/update-provider-template'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key: templateKey, models: models }),
    });
    var data = await resp.json();
    if (data.ok) {
      alert('✅ 模板已更新：' + data.model_count + ' 个模型写入 ' + (data.updated_files || []).join(', ') + '。');
    } else {
      alert('模板更新失败: ' + (data.error || '未知错误'));
    }
  } catch (e) {
    alert('模板更新出错: ' + e.message);
  }
}

// ── Model CRUD (nested inside provider) ──

function _addModel(provIdx) {
  var p = _stgProviders[provIdx];
  if (!p) return;
  if (!p.models) p.models = [];
  p.models.push({
    model_id: '', aliases: [], capabilities: ['text'], rpm: 30, cost: 0.01, thinking_default: false
  });
  _renderProvidersTab();
  // Open edit for the new model
  _editModel(provIdx, p.models.length - 1);
  // Make sure provider is expanded
  var card = document.querySelector('.stg-provider-card[data-prov-idx="' + provIdx + '"]');
  if (card) card.classList.add('expanded');
}

function _deleteModel(provIdx, modelIdx) {
  var p = _stgProviders[provIdx];
  if (!p || !p.models || !p.models[modelIdx]) return;
  var mid = p.models[modelIdx].model_id;
  if (!confirm('确定删除模型“' + (mid || '未命名') + '”吗？')) return;
  p.models.splice(modelIdx, 1);
  // Clear presets pointing to this model
  for (var k in _stgPresets) {
    if (_stgPresets[k] === mid) _stgPresets[k] = '';
  }
  _renderProvidersTab();
  _renderPresetsTab(_serverConfig);
}

function _editModel(provIdx, modelIdx) {
  var p = _stgProviders[provIdx];
  if (!p || !p.models) return;
  var m = p.models[modelIdx];
  if (!m) return;

  // Find the model card and insert edit form after it
  var card = document.querySelector('.stg-mcard[data-prov="' + provIdx + '"][data-model="' + modelIdx + '"]');
  if (!card) return;
  // Remove any existing edit forms
  var existing = card.parentElement.querySelector('.stg-edit-form');
  if (existing) existing.remove();

  var allCaps = ['text', 'vision', 'thinking', 'cheap', 'image_gen', 'embedding'];
  var html = '<div class="stg-edit-form">';
  html += '<div class="stg-edit-grid">' +
    '<div class="stg-field"><label>模型 ID</label>' +
      '<input type="text" class="stg-edit-mid" value="' + escapeHtml(m.model_id || '') + '" placeholder="如 gpt-4o, claude-opus-4"></div>' +
    '<div class="stg-field"><label>每分钟请求数 (RPM)</label>' +
      '<input type="number" class="stg-edit-rpm" value="' + (m.rpm || 30) + '" min="1"></div>' +
    '<div class="stg-field"><label>路由成本 <span class="stg-hint">（综合 $/1K，用于调度优先级）</span></label>' +
      '<input type="number" class="stg-edit-cost" value="' + (m.cost || 0.01) + '" step="0.001" min="0"></div>' +
  '</div>';

  html += '<div class="stg-field"><label>模型能力</label><div class="stg-cap-toggles">';
  for (var ci = 0; ci < allCaps.length; ci++) {
    var cap = allCaps[ci];
    var active = (m.capabilities || []).indexOf(cap) >= 0;
    html += '<button type="button" class="stg-cap-btn' + (active ? ' active' : '') + '" data-cap="' + cap + '" onclick="this.classList.toggle(\'active\')">' + cap + '</button>';
  }
  html += '</div></div>';

  html += '<div class="stg-field"><label>别名 <span class="stg-hint">（逗号分隔的替代模型 ID）</span></label>' +
    '<input type="text" class="stg-edit-aliases" value="' + escapeHtml((m.aliases || []).join(', ')) + '" placeholder="如 model-v2-b, vertex/model-v2"></div>';

  html += '<div class="stg-toggle-row"><span>默认开启思考模式</span>' +
    '<label class="stg-toggle"><input type="checkbox" class="stg-edit-think"' + (m.thinking_default ? ' checked' : '') + '>' +
    '<span class="stg-toggle-track"><span class="stg-toggle-thumb"></span></span></label></div>';

  html += '<div class="stg-edit-actions">' +
    '<button class="stg-btn-secondary" onclick="this.closest(\'.stg-edit-form\').remove()">取消</button>' +
    '<button class="stg-btn-primary" onclick="_saveModelEdit(' + provIdx + ',' + modelIdx + ')">应用</button>' +
  '</div>';
  html += '</div>';

  card.insertAdjacentHTML('afterend', html);
  // Focus model ID input
  var midInput = card.nextElementSibling.querySelector('.stg-edit-mid');
  if (midInput && !midInput.value) { midInput.focus(); }
}

function _saveModelEdit(provIdx, modelIdx) {
  var p = _stgProviders[provIdx];
  if (!p || !p.models) return;
  var m = p.models[modelIdx];
  if (!m) return;

  var form = document.querySelector('.stg-edit-form');
  if (!form) return;

  var oldModelId = m.model_id;
  m.model_id = (form.querySelector('.stg-edit-mid').value || '').trim();
  m.rpm = parseInt(form.querySelector('.stg-edit-rpm').value) || 30;
  m.cost = parseFloat(form.querySelector('.stg-edit-cost').value) || 0.01;
  m.thinking_default = form.querySelector('.stg-edit-think').checked;

  var caps = [];
  form.querySelectorAll('.stg-cap-btn.active').forEach(function(el) { caps.push(el.dataset.cap); });
  m.capabilities = caps;

  var aliasStr = (form.querySelector('.stg-edit-aliases').value || '').trim();
  m.aliases = aliasStr ? aliasStr.split(',').map(function(s) { return s.trim(); }).filter(Boolean) : [];

  // Update presets if model_id changed
  if (oldModelId && oldModelId !== m.model_id) {
    for (var k in _stgPresets) {
      if (_stgPresets[k] === oldModelId) _stgPresets[k] = m.model_id;
    }
  }

  _renderProvidersTab();
  _renderPresetsTab(_serverConfig);
}

// ── Alias CRUD (from model card chips) ──

function _addAlias(provIdx, modelIdx) {
  var p = _stgProviders[provIdx];
  if (!p || !p.models) return;
  var m = p.models[modelIdx];
  if (!m) return;
  var alias = prompt('输入别名（同一模型的替代 ID）:');
  if (!alias || !alias.trim()) return;
  if (!m.aliases) m.aliases = [];
  alias = alias.trim();
  if (m.aliases.indexOf(alias) === -1 && alias !== m.model_id) {
    m.aliases.push(alias);
    _renderProvidersTab();
  }
}

function _removeAlias(provIdx, modelIdx, aliasIdx) {
  var p = _stgProviders[provIdx];
  if (!p || !p.models) return;
  var m = p.models[modelIdx];
  if (!m || !m.aliases) return;
  m.aliases.splice(aliasIdx, 1);
  _renderProvidersTab();
}

// ══════════════════════════════════════════════════════
//  Preset Tab — visibility controls for image gen & model dropdown
// ══════════════════════════════════════════════════════

function _renderPresetsTab(cfg) {
  // Render image gen visibility toggles (same pattern as Model Dropdown)
  _renderIgVisibility();
  // Render dropdown visibility toggles
  _renderDropdownVisibility();
  // Render model defaults (fallback model, preset defaults)
  _populateModelDefaults(cfg);
}

// ══════════════════════════════════════════════════════
//  Image Generation Visibility — choose which models show in the image gen picker
// ══════════════════════════════════════════════════════

function _renderIgVisibility() {
  var container = document.getElementById('stgIgVisibility');
  if (!container) return;

  // Collect all image_gen models from enabled providers
  var igModels = _getAllModels().filter(function(entry) {
    if (entry.provider.enabled === false) return false;
    var caps = entry.model.capabilities || [];
    for (var c = 0; c < caps.length; c++) {
      if (caps[c] === 'image_gen') return true;
    }
    return false;
  });

  if (igModels.length === 0) {
    container.innerHTML = '<p class="stg-empty">未找到图片生成模型。请在服务商中添加具有 <code>image_gen</code> 能力的模型。</p>';
    return;
  }

  // Deduplicate by model_id
  var seen = {};
  var unique = [];
  for (var i = 0; i < igModels.length; i++) {
    var mid = igModels[i].model.model_id;
    if (!seen[mid]) {
      seen[mid] = true;
      unique.push(igModels[i]);
    }
  }

  // Load hidden set from server config
  var hidden = new Set((_serverConfig && _serverConfig.hidden_ig_models) || []);

  // Group by brand (same logic as _renderDropdownVisibility)
  var grouped = {};
  for (var i = 0; i < unique.length; i++) {
    var entry = unique[i];
    var brandHint = (entry.provider.name || '') + ' ' + (entry.provider.base_url || '') + ' ' + entry.model.model_id;
    var brand = entry.provider.brand || _detectBrand(brandHint);
    if (!grouped[brand]) grouped[brand] = { name: entry.provider.name || brand, models: [] };
    grouped[brand].models.push(entry.model);
  }

  var brandNames = {
    claude:'Claude', openai:'OpenAI', gemini:'Gemini', qwen:'Qwen', doubao:'Doubao',
    minimax:'MiniMax', deepseek:'DeepSeek', grok:'Grok', mistral:'Mistral', glm:'GLM',
    default:'Default', generic:'Other',
  };

  var html = '';
  for (var brand in grouped) {
    var group = grouped[brand];
    var displayName = brandNames[brand] || group.name || brand;
    html += '<div class="stg-dv-group">';
    html += '<div class="stg-dv-brand">' + _brandSvg(brand, 14) + ' <span>' + escapeHtml(displayName) + '</span></div>';
    for (var j = 0; j < group.models.length; j++) {
      var m = group.models[j];
      var mid = m.model_id;
      var isVisible = !hidden.has(mid);
      var shortName = typeof _modelShortName === 'function' ? _modelShortName(mid) : mid;
      html += '<div class="stg-dv-item">';
      html += '  <span class="stg-dv-name" title="' + escapeHtml(mid) + '">' + escapeHtml(shortName) + '</span>';
      html += '  <label class="stg-toggle stg-dv-toggle">';
      html += '    <input type="checkbox" data-ig-model-id="' + escapeHtml(mid) + '" ' + (isVisible ? 'checked' : '') + ' onchange="_onIgVisibilityChange(this)">';
      html += '    <span class="stg-toggle-track"><span class="stg-toggle-thumb"></span></span>';
      html += '  </label>';
      html += '</div>';
    }
    html += '</div>';
  }
  container.innerHTML = html;
}

function _onIgVisibilityChange(checkbox) {
  var modelId = checkbox.getAttribute('data-ig-model-id');
  var hidden = new Set((_serverConfig && _serverConfig.hidden_ig_models) || []);
  if (checkbox.checked) {
    hidden.delete(modelId);
  } else {
    hidden.add(modelId);
  }
  var arr = Array.from(hidden);
  if (_serverConfig) _serverConfig.hidden_ig_models = arr;
  // Update the global set so image gen picker reflects changes on close
  if (typeof _hiddenIgModels !== 'undefined') {
    _hiddenIgModels = hidden;
  }
}

function _toggleAllIgModels(show) {
  var container = document.getElementById('stgIgVisibility');
  if (!container) return;
  var checkboxes = container.querySelectorAll('input[type="checkbox"][data-ig-model-id]');
  var hidden = new Set();
  checkboxes.forEach(function(cb) {
    cb.checked = show;
    if (!show) hidden.add(cb.getAttribute('data-ig-model-id'));
  });
  var arr = Array.from(hidden);
  if (_serverConfig) _serverConfig.hidden_ig_models = arr;
  if (typeof _hiddenIgModels !== 'undefined') {
    _hiddenIgModels = hidden;
  }
}

// ══════════════════════════════════════════════════════
//  Model Dropdown Visibility — choose which models show in the picker
// ══════════════════════════════════════════════════════

function _renderDropdownVisibility() {
  var container = document.getElementById('stgDropdownVisibility');
  if (!container) return;

  // Collect all chat models from all enabled providers (exclude image_gen / embedding)
  var allModels = _getAllModels().filter(function(entry) {
    if (entry.provider.enabled === false) return false;
    var caps = entry.model.capabilities || ['text'];
    for (var c = 0; c < caps.length; c++) {
      if (caps[c] === 'image_gen' || caps[c] === 'embedding') return false;
    }
    return true;
  });

  if (allModels.length === 0) {
    container.innerHTML = '<p class="stg-empty">未配置聊天模型。请先添加服务商。</p>';
    return;
  }

  // Load hidden set from server config (synced at openSettings)
  var hidden = new Set((_serverConfig && _serverConfig.hidden_models) || []);

  // Group by provider brand
  var grouped = {};
  for (var i = 0; i < allModels.length; i++) {
    var entry = allModels[i];
    var brandHint = (entry.provider.name || '') + ' ' + (entry.provider.base_url || '') + ' ' + entry.model.model_id;
    var brand = entry.provider.brand || _detectBrand(brandHint);
    if (!grouped[brand]) grouped[brand] = { name: entry.provider.name || brand, models: [] };
    grouped[brand].models.push(entry.model);
  }

  var html = '';
  var brandNames = {
    claude:'Claude', openai:'OpenAI', gemini:'Gemini', qwen:'Qwen', doubao:'Doubao',
    minimax:'MiniMax', deepseek:'DeepSeek', grok:'Grok', mistral:'Mistral', glm:'GLM',
    default:'Default', generic:'Other',
  };

  for (var brand in grouped) {
    var group = grouped[brand];
    var displayName = brandNames[brand] || group.name || brand;
    html += '<div class="stg-dv-group">';
    html += '<div class="stg-dv-brand">' + _brandSvg(brand, 14) + ' <span>' + escapeHtml(displayName) + '</span></div>';
    for (var j = 0; j < group.models.length; j++) {
      var m = group.models[j];
      var mid = m.model_id;
      var isVisible = !hidden.has(mid);
      var shortName = typeof _modelShortName === 'function' ? _modelShortName(mid) : mid;
      html += '<div class="stg-dv-item">';
      html += '  <span class="stg-dv-name" title="' + escapeHtml(mid) + '">' + escapeHtml(shortName) + '</span>';
      html += '  <label class="stg-toggle stg-dv-toggle">';
      html += '    <input type="checkbox" data-model-id="' + escapeHtml(mid) + '" ' + (isVisible ? 'checked' : '') + ' onchange="_onDropdownVisibilityChange(this)">';
      html += '    <span class="stg-toggle-track"><span class="stg-toggle-thumb"></span></span>';
      html += '  </label>';
      html += '</div>';
    }
    html += '</div>';
  }
  container.innerHTML = html;
}

function _onDropdownVisibilityChange(checkbox) {
  var modelId = checkbox.getAttribute('data-model-id');
  var hidden = new Set((_serverConfig && _serverConfig.hidden_models) || []);
  if (checkbox.checked) {
    hidden.delete(modelId);
  } else {
    hidden.add(modelId);
  }
  var arr = Array.from(hidden);
  // Update cached server config so subsequent toggles are consistent
  if (_serverConfig) _serverConfig.hidden_models = arr;
  // Update the global set so dropdown reflects changes on close
  if (typeof _hiddenModels !== 'undefined') {
    _hiddenModels = hidden;
  }
}

function _toggleAllDropdownModels(show) {
  var container = document.getElementById('stgDropdownVisibility');
  if (!container) return;
  var checkboxes = container.querySelectorAll('input[type="checkbox"][data-model-id]');
  var hidden = new Set();
  checkboxes.forEach(function(cb) {
    cb.checked = show;
    if (!show) hidden.add(cb.getAttribute('data-model-id'));
  });
  var arr = Array.from(hidden);
  if (_serverConfig) _serverConfig.hidden_models = arr;
  if (typeof _hiddenModels !== 'undefined') {
    _hiddenModels = hidden;
  }
}

// ══════════════════════════════════════════════════════
//  Model Defaults — fallback model + preset defaults
// ══════════════════════════════════════════════════════

/**
 * Populate the Model Defaults section (fallback model, preset default models).
 * Uses all chat models from all enabled providers as options.
 */
function _populateModelDefaults(cfg) {
  // Collect all chat models (exclude image_gen / embedding)
  var chatModels = _getAllModels().filter(function(entry) {
    if (entry.provider.enabled === false) return false;
    var caps = entry.model.capabilities || ['text'];
    for (var c = 0; c < caps.length; c++) {
      if (caps[c] === 'image_gen' || caps[c] === 'embedding') return false;
    }
    return true;
  });

  // Deduplicate by model_id
  var seen = {};
  var uniqueModels = [];
  for (var i = 0; i < chatModels.length; i++) {
    var mid = chatModels[i].model.model_id;
    if (!seen[mid]) {
      seen[mid] = true;
      uniqueModels.push(chatModels[i]);
    }
  }

  // Read saved model_defaults from config
  var defaults = (cfg && cfg.model_defaults) || {};

  // Populate each select element
  var selectors = [
    { id: 'settingFallbackModel',  key: 'fallback_model',  emptyLabel: '（禁用自动回退）' },
    { id: 'settingDefaultModel',   key: 'default_model',   emptyLabel: '（使用环境变量）' },

  ];

  for (var s = 0; s < selectors.length; s++) {
    var sel = document.getElementById(selectors[s].id);
    if (!sel) continue;
    var savedVal = defaults[selectors[s].key] || '';

    // Clear existing options and add the empty/default option
    sel.innerHTML = '<option value="">' + selectors[s].emptyLabel + '</option>';

    // Add all available chat models
    for (var m = 0; m < uniqueModels.length; m++) {
      var modelId = uniqueModels[m].model.model_id;
      var shortName = typeof _modelShortName === 'function' ? _modelShortName(modelId) : modelId;
      var opt = document.createElement('option');
      opt.value = modelId;
      opt.textContent = shortName;
      if (modelId === savedVal) opt.selected = true;
      sel.appendChild(opt);
    }

    // If the saved value doesn't match any available model, add it as a custom entry
    if (savedVal && !seen[savedVal]) {
      var customOpt = document.createElement('option');
      customOpt.value = savedVal;
      customOpt.textContent = savedVal + ' (未注册)';
      customOpt.selected = true;
      sel.appendChild(customOpt);
    }
  }
}

/**
 * Collect current model defaults from the UI for saving.
 */
function _collectModelDefaults() {
  var result = {};
  var fields = [
    { id: 'settingFallbackModel', key: 'fallback_model' },
    { id: 'settingDefaultModel',  key: 'default_model' },

  ];
  for (var i = 0; i < fields.length; i++) {
    var el = document.getElementById(fields[i].id);
    if (el) result[fields[i].key] = el.value || '';
  }
  return result;
}

// ══════════════════════════════════════════════════════
//  Search / Advanced tabs
// ══════════════════════════════════════════════════════

function _populateSearchTab(cfg) {
  var s = cfg.search || {};
  var cb = document.getElementById('settingLlmContentFilter');
  if (cb) cb.checked = s.llm_content_filter !== false;  // default: on
  _setVal('settingFetchTopN', s.fetch_top_n || 6);
  _setVal('settingFetchTimeout', s.fetch_timeout || 15);
  _setVal('settingMaxCharsSearch', s.max_chars_search || 60000);
  _setVal('settingMaxCharsDirect', s.max_chars_direct || 200000);
  _setVal('settingMaxCharsPdf', s.max_chars_pdf || 0);
  _setVal('settingMaxBytes', s.max_bytes || 20971520);
  var sd = document.getElementById('settingSkipDomains');
  if (sd) sd.value = (s.skip_domains || []).join('\n');
}

// ══════════════════════════════════════════════════════
//  Network tab (proxy bypass)
// ══════════════════════════════════════════════════════

function _populateNetworkTab(cfg) {
  var n = cfg.network || {};

  // ── Proxy address fields (editable) ──
  _setVal('settingHttpProxy', n.http_proxy || '');
  _setVal('settingHttpsProxy', n.https_proxy || '');

  // Show env hint banner if env vars are set (so user knows the baseline)
  var envParts = [];
  if (n.env_http_proxy) envParts.push('http_proxy=' + n.env_http_proxy);
  if (n.env_https_proxy && n.env_https_proxy !== n.env_http_proxy)
    envParts.push('https_proxy=' + n.env_https_proxy);

  var envBanner = document.getElementById('proxyEnvBanner');
  var envBannerText = document.getElementById('proxyEnvBannerText');
  if (envBanner && envBannerText && envParts.length > 0) {
    envBanner.style.display = '';
    envBannerText.textContent = '系统环境变量: ' + envParts.join(' · ');
  } else if (envBanner) {
    envBanner.style.display = 'none';
  }

  // ── Unified bypass domains (editable) ──
  var pb = document.getElementById('settingProxyBypass');
  if (pb) pb.value = (n.proxy_bypass_domains || []).join('\n');

  // Show hint if env var PROXY_BYPASS_DOMAINS is set
  var hint = document.getElementById('proxyEnvHint');
  var hintText = document.getElementById('proxyEnvHintText');
  if (hint && hintText && n.env_proxy_bypass) {
    hint.style.display = '';
    hintText.textContent = '环境变量基线 (PROXY_BYPASS_DOMAINS): ' + n.env_proxy_bypass;
  } else if (hint) {
    hint.style.display = 'none';
  }
}

// ══════════════════════════════════════════════════════
//  Feishu Bot settings (in General tab → Modules)
// ══════════════════════════════════════════════════════

/** Cached Feishu config for dirty-checking restart hint */
var _feishuOrigConfig = null;

function _populateFeishuTab(cfg) {
  var f = cfg.feishu || {};
  _feishuOrigConfig = JSON.parse(JSON.stringify(f));

  // Status dot
  var dot = document.getElementById('feishuStatusDot');
  var label = document.getElementById('feishuStatusLabel');
  var desc = document.getElementById('feishuStatusDesc');
  if (dot && label && desc) {
    if (f.connected) {
      dot.textContent = '🟢'; dot.title = '已连接';
      desc.textContent = 'WebSocket 已连接 · 应用：' + (f.app_id_masked || '—');
    } else if (f.enabled) {
      dot.textContent = '🟡'; dot.title = '已启用但未连接';
      desc.textContent = '凭证已设置但未连接';
    } else {
      dot.textContent = '⚪'; dot.title = '未启用';
      desc.textContent = '请设置 App ID 和 App Secret 以启用';
    }
  }

  // Populate fields
  _setVal('settingFeishuAppId', f.app_id || '');
  // Don't populate secret — show placeholder instead
  var secretInput = document.getElementById('settingFeishuAppSecret');
  if (secretInput) {
    secretInput.value = '';
    secretInput.placeholder = f.has_secret ? '••••••••（已保存 — 留空则保持不变）' : '输入应用密钥';
  }
  _setVal('settingFeishuDefaultProject', f.default_project || '');
  _setVal('settingFeishuWorkspaceRoot', f.workspace_root || '');
  var au = document.getElementById('settingFeishuAllowedUsers');
  if (au) au.value = (f.allowed_users || []).join('\n');

  // Restart hint on credential change
  var appIdInput = document.getElementById('settingFeishuAppId');
  if (appIdInput) {
    appIdInput.oninput = _checkFeishuRestartHint;
  }
  if (secretInput) {
    secretInput.oninput = _checkFeishuRestartHint;
  }
}

function _checkFeishuRestartHint() {
  var hint = document.getElementById('feishuRestartHint');
  if (!hint || !_feishuOrigConfig) return;
  var appId = (document.getElementById('settingFeishuAppId') || {}).value || '';
  var secret = (document.getElementById('settingFeishuAppSecret') || {}).value || '';
  var changed = appId !== (_feishuOrigConfig.app_id || '') || secret.length > 0;
  hint.style.display = changed ? 'block' : 'none';
}

function _collectFeishuConfig() {
  var appId = (document.getElementById('settingFeishuAppId') || {}).value || '';
  var secret = (document.getElementById('settingFeishuAppSecret') || {}).value || '';
  var defProj = (document.getElementById('settingFeishuDefaultProject') || {}).value || '';
  var wsRoot = (document.getElementById('settingFeishuWorkspaceRoot') || {}).value || '';
  var au = (document.getElementById('settingFeishuAllowedUsers') || {}).value || '';
  var allowedUsers = au.split('\n').map(function(s) { return s.trim(); }).filter(Boolean);

  var cfg = {
    app_id: appId.trim(),
    default_project: defProj.trim(),
    workspace_root: wsRoot.trim(),
    allowed_users: allowedUsers,
  };
  // Only include secret if user typed something new
  if (secret.trim()) {
    cfg.app_secret = secret.trim();
  }
  return cfg;
}

function _populateAdvancedTab(cfg) {
  var pr = document.getElementById('settingPricing');
  if (pr && cfg.pricing) {
    var lines = [];
    for (var model in cfg.pricing) {
      var info = cfg.pricing[model];
      lines.push(model + ': in=$' + info.input + ' out=$' + info.output);
    }
    pr.value = lines.join('\n');
  }
  var si = document.getElementById('settingsServerInfo');
  if (si && cfg.server_info) {
    var html = '';
    for (var k in cfg.server_info) {
      html += '<div class="stg-info-row"><span class="stg-info-label">' + escapeHtml(k) + '</span><span class="stg-info-value">' + escapeHtml(String(cfg.server_info[k])) + '</span></div>';
    }
    si.innerHTML = html;
  }
  /* ★ Populate IndexedDB cache stats */
  _refreshCacheStatsUI();
}

/** Refresh the cache statistics display in Settings > Advanced */
function _refreshCacheStatsUI() {
  var el = document.getElementById('settingsCacheStats');
  if (!el) return;
  if (typeof ConvCache === 'undefined' || !ConvCache.isAvailable()) {
    el.textContent = 'IndexedDB 不可用';
    return;
  }
  ConvCache.stats().then(function(s) {
    el.textContent = '已缓存 ' + s.count + ' 个对话';
  });
}

/** Handler for the "Clear Cache" button in settings */
function _clearConvCacheFromSettings() {
  if (typeof ConvCache === 'undefined') return;
  var btn = document.getElementById('settingsClearCacheBtn');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ 清除中…'; }
  ConvCache.clear().then(function() {
    _refreshCacheStatsUI();
    if (btn) { btn.disabled = false; btn.textContent = '🗑 清除缓存'; }
    // Force all in-memory conversations to _needsLoad so next click refetches
    conversations.forEach(function(c) {
      if (c.id !== activeConvId) c._needsLoad = true;
    });
    if (typeof showToast === 'function') showToast('缓存已清除 — 下次点击对话时将重新从服务器加载');
  });
}

// ══════════════════════════════════════════════════════
//  Close / Save / Export / Import
// ══════════════════════════════════════════════════════

function closeSettings() {
  _stopBalancePolling();
  document.getElementById("settingsModal").classList.remove("open");
  // Refresh model dropdown to reflect any visibility changes
  if (typeof _populateModelDropdown === 'function' && typeof _registeredModels !== 'undefined' && _registeredModels.length > 0) {
    _populateModelDropdown(_registeredModels);
    _applyModelUI(config.model);
  }
  // Refresh image gen picker to reflect visibility changes
  if (typeof _loadIgModels === 'function') _loadIgModels();
}

function saveSettings() {
  // 1. Client-side config (General tab)
  config.temperature = parseFloat(document.getElementById("settingTemp").value);
  config.maxTokens = parseInt(document.getElementById("settingMaxTokens").value);
  config.imageMaxWidth = parseInt(document.getElementById("settingImageMaxWidth").value) || 0;
  config.systemPrompt = document.getElementById("settingSystem").value;
  var dtdEl = document.getElementById('settingDefaultThinkingDepth');
  if (dtdEl) {
    var oldDefault = config.defaultThinkingDepth;
    config.defaultThinkingDepth = dtdEl.value || 'off';
    // ★ Propagate: if current depth was the old default, update it to the new default
    if (config.thinkingDepth === oldDefault) {
      config.thinkingDepth = config.defaultThinkingDepth;
    }
  }
  try { localStorage.setItem("claude_client_config", JSON.stringify(config)); }
  catch (e) { debugLog('[saveSettings] localStorage save failed: ' + e.message, 'error'); }

  // 2. Feature flags (trading toggle)
  var tradingCb = document.getElementById('settingTradingEnabled');
  if (tradingCb) {
    var newVal = tradingCb.checked;
    var curVal = !!(typeof _featureFlags !== 'undefined' && _featureFlags.trading_enabled);
    if (newVal !== curVal) {
      fetch(apiUrl('/api/features'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ trading_enabled: newVal }),
      }).then(function(r) { return r.json(); }).then(function(data) {
        if (data.ok) {
          debugLog('Trading module ' + (newVal ? 'enabled' : 'disabled') + ' — applied', 'success');
          if (typeof _featureFlags !== 'undefined') _featureFlags.trading_enabled = newVal;
          var btn = document.getElementById('tradingAdvisorBtn');
          if (btn) btn.style.display = newVal ? 'flex' : 'none';
        }
      }).catch(function(e) { debugLog('Feature flag save failed: ' + e.message, 'error'); });
    }
  }

  // 3. Server config (Providers / Presets / Search)
  if (_serverConfig) {
    _saveServerConfig();
  }

  debugLog("Settings saved", "success");
  closeSettings();
}

async function _saveServerConfig() {
  // Strip empty preset mappings — especially 'opus' should never be pinned
  // to a specific version; leaving it unset lets the code default (LLM_MODEL) apply.
  var cleanPresets = {};
  for (var k in _stgPresets) {
    if (_stgPresets[k]) cleanPresets[k] = _stgPresets[k];
  }

  var payload = {
    providers: _stgProviders,
    presets: cleanPresets,
    models: {},
    search: {},
    hidden_models: (_serverConfig && _serverConfig.hidden_models) || [],
    hidden_ig_models: (_serverConfig && _serverConfig.hidden_ig_models) || [],
    model_defaults: _collectModelDefaults(),
  };
  // Search tab
  var cfCb = document.getElementById('settingLlmContentFilter');
  payload.search.llm_content_filter = cfCb ? cfCb.checked : true;
  payload.search.fetch_top_n = parseInt(document.getElementById('settingFetchTopN')?.value) || 6;
  payload.search.fetch_timeout = parseInt(document.getElementById('settingFetchTimeout')?.value) || 15;
  payload.search.max_chars_search = parseInt(document.getElementById('settingMaxCharsSearch')?.value) || 60000;
  payload.search.max_chars_direct = parseInt(document.getElementById('settingMaxCharsDirect')?.value) || 200000;
  payload.search.max_chars_pdf = parseInt(document.getElementById('settingMaxCharsPdf')?.value) || 0;
  payload.search.max_bytes = parseInt(document.getElementById('settingMaxBytes')?.value) || 20971520;
  var sd = document.getElementById('settingSkipDomains');
  if (sd) payload.search.skip_domains = sd.value.split('\n').map(function(s) { return s.trim(); }).filter(Boolean);

  // Network — proxy address config (no_proxy is auto-managed by bypass domains)
  payload.proxy_config = {
    http_proxy:  (document.getElementById('settingHttpProxy')?.value || '').trim(),
    https_proxy: (document.getElementById('settingHttpsProxy')?.value || '').trim(),
  };

  // Network — unified bypass domains (feeds both proxies_for() and no_proxy env)
  var pb = document.getElementById('settingProxyBypass');
  if (pb) {
    payload.proxy_bypass_domains = pb.value.split('\n').map(function(s) { return s.trim(); }).filter(Boolean);
  }

  // Feishu bot config
  if (typeof _collectFeishuConfig === 'function') {
    payload.feishu = _collectFeishuConfig();
  }

  try {
    var r = await fetch(apiUrl('/api/server-config'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    var data = await r.json();
    if (data.ok) {
      var msg = '服务器配置已保存，设置已实时生效。';
      debugLog('[Settings] ' + msg, 'success');
      document.getElementById('settingsStatusHint').textContent = '✅ 已保存';
      setTimeout(function() {
        var hint = document.getElementById('settingsStatusHint');
        if (hint && hint.textContent === '✅ 已保存') hint.textContent = '';
      }, 3000);
      // ★ Re-fetch server config to refresh model dropdown with any new/changed models.
      // Without this, _registeredModels stays stale and newly added providers' models
      // don't appear in the preset toggle until a page refresh.
      if (typeof _loadServerConfigAndPopulate === 'function') {
        _loadServerConfigAndPopulate();
      }
    } else {
      debugLog('[Settings] Save failed: ' + (data.error || 'unknown'), 'error');
    }
  } catch (e) {
    debugLog('[Settings] Save failed: ' + e.message, 'error');
  }
}

function exportServerConfig() {
  _loadServerConfig().then(function(cfg) {
    if (!cfg) return;
    var blob = new Blob([JSON.stringify(cfg, null, 2)], { type: 'application/json' });
    var a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'chatui-config-' + new Date().toISOString().slice(0, 10) + '.json';
    a.click();
    URL.revokeObjectURL(a.href);
    debugLog('[Settings] Config exported', 'success');
  });
}

function importServerConfig(event) {
  var file = event.target.files[0];
  if (!file) return;
  var reader = new FileReader();
  reader.onload = async function(e) {
    try {
      var imported = JSON.parse(e.target.result);
      var r = await fetch(apiUrl('/api/server-config'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(imported),
      });
      var data = await r.json();
      if (data.ok) {
        debugLog('[Settings] Config imported successfully', 'success');
        _serverConfig = null;
        openSettings();
      } else {
        debugLog('[Settings] Import failed: ' + (data.error || 'unknown'), 'error');
      }
    } catch (err) {
      debugLog('[Settings] Invalid JSON file: ' + err.message, 'error');
    }
  };
  reader.readAsText(file);
  event.target.value = '';
}


// ══════════════════════════════════════════════════════
//  OAuth Subscription Login — Browser-Centric Flow
//
//  Flow:
//    1. User clicks "登录" → fetch /api/oauth/login → get auth_url
//    2. Open auth_url in popup window (window.open)
//    3. User authenticates in popup
//    4. OAuth redirect → localhost:PORT → relay server serves HTML page
//    5. Relay page uses postMessage() to send code back to this window
//    6. We receive the code via 'message' event listener
//    7. Send code to /api/oauth/callback → server exchanges for tokens
//
//  All browser-driven. Server only does: PKCE generation + token exchange.
// ══════════════════════════════════════════════════════

// ── Global postMessage listener for OAuth callbacks ──
// The relay page (served by the server's lightweight HTTP relay) sends
// the authorization code back to us via postMessage or BroadcastChannel.
(function _initOAuthMessageListener() {
  // postMessage from popup's relay page
  window.addEventListener('message', function(event) {
    var data = event.data;
    if (!data || data.type !== 'oauth_callback') return;
    console.log('[OAuth] Received code via postMessage from relay page for:', data.provider);
    _handleOAuthCode(data.provider, data.code);
  });

  // BroadcastChannel fallback (works even if popup loses window.opener ref)
  try {
    var bc = new BroadcastChannel('oauth_callback');
    bc.onmessage = function(event) {
      var data = event.data;
      if (!data || data.type !== 'oauth_callback') return;
      console.log('[OAuth] Received code via BroadcastChannel for:', data.provider);
      _handleOAuthCode(data.provider, data.code);
    };
  } catch(e) {
    // BroadcastChannel not supported — postMessage still works
  }
})();

// ── Handle received OAuth code ──
function _handleOAuthCode(provider, code, state) {
  if (!provider || !code) return;

  var capProvider = provider === 'codex' ? 'Codex' : 'Claude';
  _updateOAuthCard(provider, { status: 'exchanging' });

  // Send code to server for token exchange
  // Try POST first; if proxy returns 405, fall back to GET with query params
  var body = { provider: provider, code: code };
  if (state) body.state = state;
  function _doCallbackRequest(useGet) {
    if (useGet) {
      console.warn('[OAuth] POST got 405, retrying as GET for /api/oauth/callback');
      var qs = 'provider=' + encodeURIComponent(provider) + '&code=' + encodeURIComponent(code);
      if (state) qs += '&state=' + encodeURIComponent(state);
      return fetch(apiUrl('/api/oauth/callback?' + qs));
    }
    return fetch(apiUrl('/api/oauth/callback'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  }
  _doCallbackRequest(false)
    .then(function(r) {
      if (r.status === 404 || r.status === 405) return _doCallbackRequest(true);
      return r;
    })
    .then(function(r) {
      if (!r.ok) return r.text().then(function(t) { throw new Error(t.slice(0, 200)); });
      return r.json();
    })
    .then(function(data) {
      if (data.error) {
        _updateOAuthCard(provider, { status: 'error' });
        alert('Token 交换失败: ' + data.error);
        return;
      }
      // Success!
      _updateOAuthCard(provider, { status: 'success', authenticated: true, email: data.email || '' });
      _autoConfigureOAuthProvider(provider, { email: data.email });

      // Hide manual fallback
      var manualDiv = document.getElementById('oauth' + capProvider + 'Manual');
      if (manualDiv) manualDiv.style.display = 'none';
    })
    .catch(function(e) {
      console.error('[OAuth] Token exchange error:', e);
      _updateOAuthCard(provider, { status: 'error' });
      alert('Token 交换失败: ' + e.message);
    });
}

function _loadOAuthStatus() {
  fetch(apiUrl('/api/oauth/status'))
    .then(function(r) { return r.json(); })
    .then(function(data) {
      _updateOAuthCard('claude', data.claude);
      _updateOAuthCard('codex', data.codex);
    })
    .catch(function(e) {
      console.warn('[OAuth] Failed to load status:', e);
    });
}

function _updateOAuthCard(provider, status) {
  if (!status) return;
  var capProvider = provider === 'codex' ? 'Codex' : 'Claude';
  var badge = document.getElementById('oauth' + capProvider + 'Status');
  var info = document.getElementById('oauth' + capProvider + 'Info');
  var email = document.getElementById('oauth' + capProvider + 'Email');
  var loginBtn = document.getElementById('oauth' + capProvider + 'LoginBtn');
  var logoutBtn = document.getElementById('oauth' + capProvider + 'LogoutBtn');

  if (!badge) return;

  if (status.authenticated) {
    badge.textContent = '已登录';
    badge.className = 'oauth-status-badge authenticated';
    if (info) { info.style.display = ''; }
    if (email) { email.textContent = status.email || '(unknown)'; }
    if (loginBtn) { loginBtn.style.display = 'none'; }
    if (logoutBtn) { logoutBtn.style.display = ''; }
  } else if (status.status === 'started' || status.status === 'waiting_callback' || status.status === 'exchanging') {
    badge.textContent = status.status === 'exchanging' ? '正在获取 Token…' : '等待授权…';
    badge.className = 'oauth-status-badge pending';
    if (info) { info.style.display = 'none'; }
    // Show a cancel/retry button so users aren't stuck forever
    if (loginBtn) {
      loginBtn.disabled = false;
      loginBtn.textContent = '取消 / 重试';
      loginBtn.onclick = function() { _oauthCancelAndRetry(provider); };
    }
    if (logoutBtn) { logoutBtn.style.display = 'none'; }
  } else if (status.status === 'error') {
    badge.textContent = '错误';
    badge.className = 'oauth-status-badge error';
    if (info) { info.style.display = 'none'; }
    if (loginBtn) { loginBtn.disabled = false; loginBtn.textContent = provider === 'codex' ? '登录 ChatGPT' : '登录 Claude'; loginBtn.onclick = function() { _oauthLogin(provider); }; }
    if (logoutBtn) { logoutBtn.style.display = 'none'; }
  } else {
    badge.textContent = '未登录';
    badge.className = 'oauth-status-badge';
    if (info) { info.style.display = 'none'; }
    if (loginBtn) { loginBtn.disabled = false; loginBtn.textContent = provider === 'codex' ? '登录 ChatGPT' : '登录 Claude'; loginBtn.style.display = ''; loginBtn.onclick = function() { _oauthLogin(provider); }; }
    if (logoutBtn) { logoutBtn.style.display = 'none'; }
  }
}

function _oauthCancelAndRetry(provider) {
  var capProvider = provider === 'codex' ? 'Codex' : 'Claude';
  // Call logout to reset the server-side flow state
  fetch(apiUrl('/api/oauth/logout'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ provider: provider }),
  }).catch(function() {});
  // Reset UI immediately
  _updateOAuthCard(provider, { status: 'not_started', authenticated: false });
  // Restore normal onclick
  var loginBtn = document.getElementById('oauth' + capProvider + 'LoginBtn');
  if (loginBtn) {
    loginBtn.onclick = function() { _oauthLogin(provider); };
  }
  // Hide manual paste box
  var manualDiv = document.getElementById('oauth' + capProvider + 'Manual');
  if (manualDiv) manualDiv.style.display = 'none';
}

function _oauthLogin(provider) {
  var capProvider = provider === 'codex' ? 'Codex' : 'Claude';
  var loginBtn = document.getElementById('oauth' + capProvider + 'LoginBtn');
  if (loginBtn) { loginBtn.disabled = true; loginBtn.textContent = '正在准备…'; }

  // Step 1: Ask server to generate PKCE + auth URL + start relay server
  // Try POST first; if proxy returns 404/405, fall back to GET with query params
  // (VSCode tunnel proxies may not forward POST to unknown paths)
  function _doLoginRequest(useGet) {
    if (useGet) {
      console.warn('[OAuth] POST failed, retrying as GET for /api/oauth/login');
      return fetch(apiUrl('/api/oauth/login?provider=' + encodeURIComponent(provider)));
    }
    return fetch(apiUrl('/api/oauth/login'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider: provider }),
    });
  }
  _doLoginRequest(false)
    .then(function(r) {
      if (r.status === 404 || r.status === 405) return _doLoginRequest(true);
      return r;
    })
    .then(function(r) {
      if (!r.ok) {
        return r.text().then(function(t) { throw new Error('HTTP ' + r.status + ': ' + t.slice(0, 200)); });
      }
      return r.json();
    })
    .then(function(data) {
      if (data.error) {
        alert('OAuth 登录失败: ' + data.error);
        if (loginBtn) { loginBtn.disabled = false; loginBtn.textContent = provider === 'codex' ? '登录 ChatGPT' : '登录 Claude'; }
        return;
      }

      // Step 2: Open the auth URL in a popup window
      // For Claude: redirects to console.anthropic.com which shows code#state
      // For Codex: redirects to localhost relay which auto-sends via postMessage
      var popup = null;
      if (data.auth_url) {
        var w = 600, h = 700;
        var left = (screen.width - w) / 2, top = (screen.height - h) / 2;
        popup = window.open(data.auth_url, 'oauth_' + provider,
          'width=' + w + ',height=' + h + ',left=' + left + ',top=' + top +
          ',menubar=no,toolbar=no,status=no,scrollbars=yes');

        if (!popup || popup.closed) {
          // Popup blocked — fall back to new tab
          popup = null;
          window.open(data.auth_url, '_blank');
        }
      }

      // Update UI to waiting state
      _updateOAuthCard(provider, { status: 'waiting_callback' });

      // Show manual paste box immediately with auth URL for copy
      // (Chinese users need to copy the URL to a proxied browser)
      var manualDiv = document.getElementById('oauth' + capProvider + 'Manual');
      if (manualDiv) {
        manualDiv.style.display = '';
        var authUrlInput = document.getElementById('oauth' + capProvider + 'AuthUrl');
        if (authUrlInput && data.auth_url) authUrlInput.value = data.auth_url;
      }

      // ── Detect popup closed → auto-reset ONLY if manual box not used ──
      if (popup) {
        var popupCheckInterval = setInterval(function() {
          if (!popup || popup.closed) {
            clearInterval(popupCheckInterval);
            // Don't reset if manual paste box is visible (user may be pasting code)
            var manualInput = document.getElementById('oauth' + capProvider + 'ManualUrl');
            if (manualInput && manualInput.value.trim()) return;  // user is typing
            // Only reset if still in waiting state (not already succeeded)
            var badge = document.getElementById('oauth' + capProvider + 'Status');
            if (badge && (badge.textContent.indexOf('等待') >= 0 || badge.textContent.indexOf('授权') >= 0)) {
              // Don't reset — just update button to allow retry
              var loginBtn2 = document.getElementById('oauth' + capProvider + 'LoginBtn');
              if (loginBtn2) {
                loginBtn2.disabled = false;
                loginBtn2.textContent = '重新打开弹窗';
                loginBtn2.onclick = function() {
                  // Re-open popup with same auth URL, don't create new flow
                  var w2 = 600, h2 = 700;
                  var left2 = (screen.width - w2) / 2, top2 = (screen.height - h2) / 2;
                  window.open(data.auth_url, 'oauth_' + provider,
                    'width=' + w2 + ',height=' + h2 + ',left=' + left2 + ',top=' + top2 +
                    ',menubar=no,toolbar=no,status=no,scrollbars=yes');
                };
              }
            }
          }
        }, 1000);
      }
    })
    .catch(function(e) {
      console.error('[OAuth] Login error:', e);
      alert('OAuth 登录请求失败: ' + e.message);
      if (loginBtn) { loginBtn.disabled = false; loginBtn.textContent = provider === 'codex' ? '登录 ChatGPT' : '登录 Claude'; }
    });
}

function _oauthLogout(provider) {
  if (!confirm('确定要退出 ' + (provider === 'codex' ? 'ChatGPT' : 'Claude') + ' 订阅登录吗？')) return;

  // Try POST first; if proxy returns 405, fall back to GET with query params
  function _doLogoutRequest(useGet) {
    if (useGet) {
      console.warn('[OAuth] POST failed, retrying as GET for /api/oauth/logout');
      return fetch(apiUrl('/api/oauth/logout?provider=' + encodeURIComponent(provider)));
    }
    return fetch(apiUrl('/api/oauth/logout'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider: provider }),
    });
  }
  _doLogoutRequest(false)
    .then(function(r) {
      if (r.status === 404 || r.status === 405) return _doLogoutRequest(true);
      return r;
    })
    .then(function(r) { return r.json(); })
    .then(function() {
      _updateOAuthCard(provider, { status: 'not_started', authenticated: false });
    })
    .catch(function(e) {
      alert('退出失败: ' + e.message);
    });
}

function _oauthManualSubmit(provider) {
  var capP = provider === 'codex' ? 'Codex' : 'Claude';
  var input = document.getElementById('oauth' + capP + 'ManualUrl');
  if (!input || !input.value.trim()) {
    alert('请粘贴授权码或回调 URL');
    return;
  }
  var val = input.value.trim();

  // Support multiple formats:
  // 1. Full callback URL: http://localhost:PORT/callback?code=XXX&state=YYY
  // 2. code#state format (shown by Anthropic console after auth)
  // 3. Raw authorization code
  var body = { provider: provider };
  if (val.indexOf('http') === 0) {
    body.callback_url = val;
  } else if (val.indexOf('#') > 0) {
    // code#state format from Anthropic console
    var parts = val.split('#');
    body.code = parts[0];
    body.state = parts[1] || '';
  } else {
    body.code = val;
  }

  // Try POST first; if proxy returns 405, fall back to GET with query params
  function _doManualCallbackRequest(useGet) {
    if (useGet) {
      console.warn('[OAuth] POST got 405, retrying as GET for /api/oauth/callback (manual)');
      var qs = 'provider=' + encodeURIComponent(body.provider);
      if (body.code) qs += '&code=' + encodeURIComponent(body.code);
      if (body.state) qs += '&state=' + encodeURIComponent(body.state);
      if (body.callback_url) qs += '&callback_url=' + encodeURIComponent(body.callback_url);
      return fetch(apiUrl('/api/oauth/callback?' + qs));
    }
    return fetch(apiUrl('/api/oauth/callback'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  }
  _doManualCallbackRequest(false)
    .then(function(r) {
      if (r.status === 404 || r.status === 405) return _doManualCallbackRequest(true);
      return r;
    })
    .then(function(r) {
      if (!r.ok) return r.text().then(function(t) { throw new Error(t.slice(0, 200)); });
      return r.json();
    })
    .then(function(data) {
      if (data.error) {
        alert('授权失败: ' + data.error);
      } else {
        _updateOAuthCard(provider, { status: 'success', authenticated: true, email: data.email || '' });
        var manualDiv = document.getElementById('oauth' + capP + 'Manual');
        if (manualDiv) manualDiv.style.display = 'none';
        input.value = '';
        _autoConfigureOAuthProvider(provider, { email: data.email });
      }
    })
    .catch(function(e) {
      alert('提交失败: ' + e.message);
    });
}

function _autoConfigureOAuthProvider(provider, status) {
  var name = provider === 'codex' ? 'ChatGPT Plus' : 'Claude Pro';
  var el = document.getElementById('settingsStatusHint');
  if (el) {
    el.textContent = '✅ ' + name + ' 登录成功！请在「服务商」标签页添加对应模型。';
    el.style.color = '#28a745';
  }
}
