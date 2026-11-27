export const ROLE = {overview:"全景", detail:"细部", reference:"参考", repeat:"重拍"};
export const STATE = {unread:"未抄", identified:"已抄", empty:"空格", covered:"遮挡"};
export const PURPOSE = {retention:"留样", refire:"返烧对照", review:"复查"};
export const ROTATION_LABEL = {0:"0°", 90:"90°", 180:"180°", 270:"270°"};
export const STRATEGY = {glaze:"釉面优先", number:"逐格号证优先", balanced:"兼顾（默认）"};

export const measure = (key, label, value, min, max, step = "any") => ({key,label,value,min,max,step,type:"number"});
export const toggle = (key, label, value) => ({key,label,value,type:"checkbox"});
export const menu = (key, label, options, value) => ({key,label,options,value,type:"select"});
export const string = (key, label, value) => ({key,label,value,type:"text"});
export const selectPhoto = {key:"comparison",label:"对照照片",type:"photo"};

export const SHEETS = {
  "颜色与质地": {
    color: {label:"样区读色", help:"在原照框出试片表面。亮度以线性 sRGB 计算。", fields:[measure("bins","直方图档数",24,8,64,1)], region:true},
    calibration:{label:"校色参考",help:"将框选区域用作已知色片。可在参数中加入其它色片。",fields:[string("expected","参考色值","#9a9588")],type:"calibration",json:true},
    target:{label:"采集参考检查",help:"为同一张图指定黑、白或中性色片。检查裁切、色偏和采集溢出。",fields:[toggle("chart","显示色片间距图",false)],type:"target",json:true},
    texture:{label:"质地分布",help:"比较两种观察尺度上的明暗起伏。",fields:[measure("fine","细尺度",1.3,.6,10),measure("coarse","粗尺度",5.5,1,25)],region:true},
    palette:{label:"样片调色条",help:"输入参与的照片和样区，可生成按覆盖量或等宽的色条。",fields:[toggle("uniform","等宽色样",true),toggle("contact","输出带色签接触片",false)],type:"palette",json:true},
    cells:{label:"釉色一致性",help:"矩形样区分格，各格和整体颜色比较。",fields:[measure("rows","行数",3,2,8,1),measure("columns","列数",3,2,8,1),measure("tolerance","单格 ΔE 容差",5,.5,40)],region:true},
    phases:{label:"釉相分布",help:"两块或多块已知呈色作为参考。条纹区域含多种可能。",fields:[measure("gap","参照最小间距",1.5,.01,15),measure("minimum_area","连片大小",8,1,8000),toggle("chart","显示各相面积图",false)],type:"phases",region:true,json:true},
    grades:{label:"样区评级",help:"将当前框区与用户指定的实物颜色参照比较。",fields:[measure("max_delta","允许色差",14,1,50),measure("min_gap","评级分辨间距",1,0,25)],type:"grades",json:true},
  },
  "表面与缺陷": {
    pinholes:{label:"针孔检查",help:"暗部相对周边的局部对比。高亮提示待人工复核的位置。",fields:[measure("contrast","暗部对比",.11,.01,.75),measure("neighborhood","周边半径",5,1,32),measure("minimum_area","最小面积",3,1,200),measure("maximum_area","最大面积",110,3,5000),menu("connectivity","连通方式",{eight:"八邻",four:"四邻"},"eight")],region:true},
    glare:{label:"高光识别",help:"检查实底中高亮低彩区域，记录照片反光影响。",fields:[measure("brightness","亮度分界",.81,.2,.99),measure("max_chroma","最大彩度",14,1,80),measure("contrast","局部对比",.08,0,.5)],region:true},
    crazing:{label:"开片方向检查",help:"从暗纹中心估计方向、长度和交叉。",fields:[measure("floor","暗纹强度",.018,.002,.23),measure("scale","纹线宽度",1.45,.8,7),measure("minimum","最小连片",12,3,2300),toggle("chart","显示开片方向图",false)],region:true},
    marks:{label:"缺陷图",help:"顺序标点后选择轮廓、裂纹路径或单点。可继续补其它标注。",fields:[string("name","标注名称","表面复查"),menu("category","缺陷类别",{crack:"裂纹",crawling:"缩釉",blister:"鼓泡",pinholes:"针孔",stain:"色斑",unknown:"未定"},"crack"),menu("shape","标注形状",{trace:"折线",polygon:"封闭轮廓",point:"单点"},"trace"),menu("outcome","后续操作",{observe:"观察",test:"复测",hold:"留置"},"observe"),measure("severity","等级",2,1,4,1)],type:"marks",json:true},
    spot:{label:"斑点读色",help:"标记斑点中心，比较内圆与周边环带。",fields:[measure("radius","中心半径",18,2,160)],type:"spot"},
    coverage:{label:"实底遮挡",help:"检查透明底、部分覆盖和最大实底内圈。",fields:[measure("alpha_threshold","有效像素阈值",.95,.1,1)],region:true},
    cutout:{label:"底板透底",help:"用亮度区分实物和底板，输出可独立保存的透底图。",fields:[menu("polarity","实物亮度",{dark:"较暗",light:"较亮"},"dark"),measure("minimum_island","最小实物区",18,1,5000),measure("fill_holes","补孔面积上限",0,0,3000),measure("feather","边缘柔化",.7,0,7)],region:true},
    mask:{label:"遮挡编辑",help:"标点画出要遮去的非试片区域。高级设置可加入恢复笔迹。",fields:[menu("action","操作",{erase:"遮去",restore:"恢复"},"erase"),menu("shape","形状",{polygon:"轮廓",brush:"笔刷"},"polygon"),measure("radius","笔刷宽度",12,1,160),measure("strength","强度",1,.01,1)],type:"mask",json:true},
    recolor:{label:"高光减弱",help:"以高光周围颜色作局部估计，并给照片实底留保护边。",fields:[measure("strength","高光减弱",.6,.05,1),measure("threshold","高光阈值",.85,.42,.99),measure("radius","补色半径",9,1.5,36),measure("protect","边缘保护",3,0,32)],region:true},
  },
  "几何与对照": {
    ruler:{label:"标尺",help:"在原图标两个尺度点，填写它们之间的实物长度。归档后用于后续测量。",fields:[measure("millimeters","实物长度（mm）",35,.01,10000)],type:"line"},
    line:{label:"测线",help:"先标两个点，查看线上亮度、原图位置及无实底间隙。",fields:[measure("spacing","采样间距",3,.5,90)],type:"line"},
    traverse:{label:"折线读色",help:"顺序标记折线路径节点，逐段读色。",fields:[measure("spacing","采样间距",3,.5,40)],type:"points"},
    crop:{label:"细部裁片",help:"框出细部样区，原图保留。可切到旋转裁片。",fields:[menu("style","裁片方式",{region:"样区",rotated:"旋转裁片"},"region"),measure("max_side","最大长边",900,100,1800,1),measure("border","透明边",0,0,120,1),measure("width","旋转裁片宽",250,4,1800,1),measure("height","旋转裁片高",220,4,1800,1),measure("angle","旋转角",0,-180,180),toggle("flatten","转为浅纸底",false)],type:"crop",region:true},
    rectify:{label:"试片校直",help:"以左上、右上、右下、左下的顺序标出实物四角。",fields:[measure("width_mm","实物宽 mm",80,1,800),measure("height_mm","实物高 mm",50,1,800),measure("pixels_per_mm","每毫米像素",7,1,22),measure("padding_mm","环境余量 mm",0,0,20),menu("canvas","背景",{transparent:"透明",cream:"浅纸",black:"深底"},"transparent"),toggle("mirror","以背面看",false),toggle("grid","附 10 mm 网尺",false)],type:"rectify"},
    layer:{label:"剖面厚度",help:"填写沿界面方向、向内方向和两条边界控制点。内容可从原照顺序标点合成。",fields:[measure("spacing","站点间距",6,.75,50),measure("across_samples","跨层取样数",16,6,64,1)],type:"layer",json:true},
    comparison:{label:"共视差图",help:"原照与对照各用三个对应参照点。差图在原照坐标展示共同实底。",fields:[selectPhoto,measure("threshold","ΔE 阈值",8,.5,90),measure("minimum_area","最小片区",12,1,8000)],type:"pair",region:true,json:true},
    overlay:{label:"两片叠照",help:"原照与对照各用三个对应参照点。",fields:[selectPhoto,measure("opacity","叠照比例",.5,0,1)],type:"pair",region:true,json:true},
    relief:{label:"双向掠射图",help:"选一对相反灯向拍的图，输入三对对应点，可查看浮凸提示。",fields:[selectPhoto,measure("light_elevation","灯光离台角度",37,8,78),measure("slope_threshold","提示角度",6,.5,30),menu("output","输出",{tilt:"相对坡度",reflection:"反射光",confidence:"证据实底"},"tilt"),toggle("legend","色标",false)],type:"pair",region:true,json:true},
    recipe:{label:"返烧配方阵列",help:"填基础釉及两个混合料。测量格按 A1、A2 的方式关联照片样区。",fields:[measure("rows","纵格数",4,2,6,1),measure("columns","横格数",4,2,6,1),measure("row_limit","纵向最高比例 %",40,.01,100),measure("column_limit","横向最高比例 %",40,.01,100),measure("grams","每格克数",20,2,500),measure("precision","秤可读精度",.1,.01,2)],type:"recipe",json:true}
  }
};

export const SPECS = Object.assign({}, ...Object.values(SHEETS));
export const LABELS = {...Object.fromEntries(Object.entries(SPECS).map(([k,v])=>[k,v.label])),
  tray:"托盘抄录",cards:"留样卡",board:"对照展板",packet:"物证封存",catalog:"已抄录图册",
  closeout:"留档检核单",selection:"保存的影像位置",reconcile:"托盘拼合抄录",refire:"复烧袋留样卡"};

export const CSV_KINDS = new Set([
  "color","comparison","texture","glare","pinholes","spot","ruler","line","marks","grades","coverage","palette",
  "traverse","cells","recipe","crazing","phases","rectify","relief","target","layer","closeout",
]);

const shrink = (photo,x0,y0,x1,y1) => [x0*photo.width,y0*photo.height,x1*photo.width,y1*photo.height].map(Math.round);
export function anchorPoints(photo) {
  return [[.13,.18],[.84,.18],[.13,.79]].map(([x,y])=>[Math.round(x*photo.width),Math.round(y*photo.height)]);
}
export function centered(photo) {return shrink(photo,.13,.18,.84,.79);}
function ascending(photo) {
  return [[.2,.38],[.8,.38],[.2,.51],[.8,.51],[.2,.66],[.8,.66]]
    .map(([x,y])=>[Math.round(x*photo.width),Math.round(y*photo.height)]);
}

export function extendSettings(code, fields, photo, history, region, points) {
  const spec = SPECS[code];
  const current = region || centered(photo);
  const coordinates = points.length ? points : anchorPoints(photo);
  const cornerRect = [[current[0],current[1]],[current[2],current[1]],[current[2],current[3]],[current[0],current[3]]];
  const first = shrink(photo,.15,.24,.23,.32);
  const second = shrink(photo,.62,.24,.69,.32);
  if (spec.region && region) fields.region = region;
  if (spec.type === "calibration") {
    fields.references = [{name:"参考片",expected:fields.expected,region:current}];
    delete fields.expected;
  } else if (spec.type === "target") {
    fields.targets = [{name:"中性参考",region:current,mode:"neutral"}];
  } else if (spec.type === "palette") {
    fields.items = [{photo:photo.id,region:first,label:"色片甲"},{photo:photo.id,region:second,label:"色片乙"}];
  } else if (spec.type === "phases" || spec.type === "grades") {
    const entries = [{name:"相甲",region:first},{name:"相乙",region:second}];
    if (spec.type === "phases") fields.phases = entries;
    else {fields.targets = entries; fields.regions = [{name:"当前样区",region:current}];}
  } else if (spec.type === "marks") {
    const accepted = fields.shape === "point" ? coordinates.slice(0,1) : coordinates;
    fields.marks = [{name:fields.name,kind:fields.category,shape:fields.shape,
      outcome:fields.outcome,severity:fields.severity,points:accepted}];
    for (const key of ["name","category","shape","outcome","severity"]) delete fields[key];
  } else if (spec.type === "spot") {
    fields.center = points[0] || [(current[0]+current[2])/2,(current[1]+current[3])/2];
  } else if (spec.type === "mask") {
    fields.strokes = [{points:coordinates,action:fields.action,shape:fields.shape,radius:fields.radius,strength:fields.strength}];
    for (const key of ["action","shape","radius","strength"]) delete fields[key];
  } else if (spec.type === "line") {
    fields.start = coordinates[0];
    fields.end = coordinates[1];
  } else if (spec.type === "points") {
    fields.points = coordinates;
  } else if (spec.type === "crop" && fields.style === "rotated") {
    fields.center = points[0] || [(current[0]+current[2])/2,(current[1]+current[3])/2];
  } else if (spec.type === "rectify") {
    fields.points = points.length ? points : cornerRect;
    if (fields.grid) fields.grid = {};
    else delete fields.grid;
  } else if (spec.type === "layer") {
    const steps = points.length >= 7 ? points : ascending(photo);
    fields.origin = steps[0]; fields.tangent = steps[1]; fields.inward = steps[2];
    fields.outer = points.length >= 7 ? steps.slice(3,5) : steps.slice(0,2);
    fields.inner = points.length >= 7 ? steps.slice(5) : steps.slice(4,6);
  } else if (spec.type === "pair") {
    const other = history.find(item=>item.id===fields.comparison);
    fields.left_points = points.length >= 3 ? points.slice(0,3) : anchorPoints(photo);
    if (other) fields.right_points = points.length >= 6 ? points.slice(3,6) : anchorPoints(other);
  } else if (spec.type === "recipe") {
    fields.base = {name:"基础釉",materials:[{name:"长石",parts:"60"},{name:"石英",parts:"40"}]};
    fields.row_mix = {name:"纵向料",materials:[{name:"木灰",parts:"100"}]};
    fields.column_mix = {name:"横向料",materials:[{name:"氧化铁",parts:"30"},{name:"长石",parts:"70"}]};
    fields.readings = [{address:"A1",photo:photo.id,region:first},{address:"A2",photo:photo.id,region:second}];
  }
  return fields;
}
