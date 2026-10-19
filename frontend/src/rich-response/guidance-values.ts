/** Declared human answers, independent from DOM, network and execution. */
export interface GuidanceQuestion { question:string; options:string[]; descriptions?:string[] }
export function validGuidanceAnswer(mode:string|undefined,options:string[],questions:GuidanceQuestion[]|undefined,response:string):boolean{
  if(typeof response!=='string'||!response.trim()||response.length>32768)return false;
  if(!mode||mode==='choice')return options.includes(response);
  if(mode==='free_text')return true;
  if(mode!=='batch'||!questions?.length)return false;
  try{
    const answers=JSON.parse(response) as Array<{index:number;answer:string}>;
    return Array.isArray(answers)&&answers.length===questions.length
      &&new Set(answers.map(value=>value?.index)).size===answers.length
      &&answers.every(value=>value&&Number.isInteger(value.index)&&value.index>=0&&value.index<questions.length
        &&typeof value.answer==='string'&&Boolean(value.answer.trim())
        &&(!questions[value.index].options.length||questions[value.index].options.includes(value.answer)));
  }catch{return false;}
}
