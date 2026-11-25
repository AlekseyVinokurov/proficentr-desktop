"""T06: адаптация именно пяти пользовательских комплектов; ZIP остаются локально."""
from pathlib import Path
from zipfile import ZipFile
from io import BytesIO
import re,json,hashlib
root=Path('src/proficentr/resources/working_templates');root.mkdir(parents=True,exist_ok=True)
common={'organization_full_name':'center.full_name','director_name':'center.director_name','director_full_name':'center.director_name','director_position':'center.director_position','organization_address':'center.legal_address','organization_inn':'center.inn','organization_kpp':'center.kpp','organization_ogrn':'center.ogrn','license_number':'center.license_number','license_date':'center.license_date','license_authority':'center.license_authority','program_name':'program.name','program_hours':'program.hours','student_full_name':'student.full_name','student_address':'student.registration_address','student_phone':'student.phone','start_date':'training.start_date','end_date':'training.end_date','study_start_date':'training.start_date','study_end_date':'training.end_date','contract_number':'document.number','contract_date':'document.date','order_number':'document.number','order_date':'document.date','protocol_number':'protocol.number','protocol_date':'protocol.date','document_series':'issued.blank_series','document_number':'issued.blank_number','registration_number':'issued.number','issue_date':'issued.date','consent_expiry_date':'admission.valid_until','identity_issued_date':'student.passport_issue_date','identity_issuer':'student.passport_issued_by','contract_total':'contract.total_price','payment_terms':'contract.payment_terms','customer_full_name':'customer.full_name','customer_address':'customer.legal_address','customer_inn':'customer.inn','customer_kpp':'customer.kpp','customer_representative':'customer.representative_name','customer_authority':'customer.authority_basis','awarded_qualification':'issued.qualification','attestation_result':'work.attestation_result','group_name':'group.name'}
rowmap={'student_full_name':'full_name','student_address':'registration_address','student_phone':'phone','study_start_date':'start_date','study_end_date':'end_date','start_date':'start_date','end_date':'end_date','attestation_result':'attestation_result','awarded_qualification':'qualification','protocol_number':'protocol_number','protocol_date':'protocol_date'}
manifest=[];extra=set();rowextra=set()
archives=[('admission','Комплект_шаблонов_приема.zip'),('contracts','Комплект_договоров_обучения.zip'),('orders','Комплект_шаблонов_приказов.zip'),('protocols','Комплект_шаблона_протоколов.zip'),('issued','Комплект_выпускных_документов.zip')]
for cat,filename in archives:
 with ZipFile(Path('templates')/filename) as z:
  items=sorted((i for i in z.infolist() if i.filename.endswith('.docx')),key=lambda i:i.filename)
  for idx,item in enumerate(items,1):
   try:name=item.filename.encode('cp437').decode('utf-8')
   except UnicodeError:name=item.filename
   code=f'{cat}_{idx:02d}';typ='other';variant=code;direction='po' if name.startswith('ПО_') else 'dpo' if name.startswith('ДПО_') else '';group=False
   if cat=='contracts':typ='contract_person' if 'ФЛ' in name else 'contract_legal';variant=direction;group=typ=='contract_legal'
   if cat=='orders':typ='enrollment_order' if 'Зачисление' in name else 'completion_order' if 'Завершение' in name else 'expulsion_order';variant=direction;group=True
   if cat=='protocols':typ='protocol_'+direction;variant=direction;group=True
   if cat=='admission':
    n=int(name[:2]);typ='admission_checklist' if n<=3 else 'application' if n<=5 else 'consent'
    variant=['dpo_upskilling','dpo_retraining','po','admission-dpo','admission-po','admission'][n-1]
   if cat=='issued':
    n=int(name[:2]);typ=['dpo_upskilling_certificate','dpo_retraining_diploma','po_profession_certificate','study_certificate','issuance_registry','delivery_receipt'][n-1];variant='standard';group=n>=5
   data=z.read(item);out=BytesIO();variables=set();original_variables=set()
   def substitute(xml,row=False,collection='students'):
    def tag(m):
     key=m.group(1).strip();original_variables.add(key)
     if row:
      field=rowmap.get(key,key) if collection=='students' else key
      if collection=='students' and field not in ('full_name','registration_address','phone','start_date','end_date','attestation_result','qualification','protocol_number','protocol_date'):rowextra.add(field)
      path='row.'+field
     else:path=common.get(key,'work.'+key)
     if path.startswith('work.'):extra.add(path[5:])
     variables.add((collection+'.' if row else '')+path[4:] if row else path)
     return '{{ '+path+' }}'
    return re.sub(r'{{\s*(\w+)\s*}}',tag,xml)
   with ZipFile(BytesIO(data)) as source, ZipFile(out,'w') as dest:
    for part in source.infolist():
     content=source.read(part)
     if part.filename.startswith('word/') and part.filename.endswith('.xml'):
      xml=content.decode()
      def rowreplace(m):
       tr=m.group();tags=set(re.findall(r'{{\s*(\w+)\s*}}',tr))
       collection=None
       if 'member_full_name' in tags:collection='commission'
       elif cat=='issued' and name.startswith('04_') and 'module_name' in tags:collection='modules'
       elif cat=='issued' and int(name[:2])>=5 and ('registration_number' in tags):collection='documents'
       elif group and ('row_number' in tags or (cat=='contracts' and 'program_name' in tags and 'training_price' in tags)):collection='students'
       if collection:
        start='<w:tr><w:tc><w:p><w:r><w:t>{%tr for row in '+collection+' %}</w:t></w:r></w:p></w:tc></w:tr>'
        end='<w:tr><w:tc><w:p><w:r><w:t>{%tr endfor %}</w:t></w:r></w:p></w:tc></w:tr>'
        return start+substitute(tr,True,collection)+end
       return tr
      xml=re.sub(r'<w:tr(?:\s[^>]*)?>.*?</w:tr>',rowreplace,xml,flags=re.S)
      xml=substitute(xml)
      content=xml.encode()
     dest.writestr(part,content)
   destpath=root/cat/(code+'.docx');destpath.parent.mkdir(exist_ok=True);destpath.write_bytes(out.getvalue())
   title=name[:-5].replace('_',' ')
   if cat=='admission':title={'01_Перечень':'Перечень документов — ДПО ПК','02_Перечень':'Перечень документов — ДПО ПП','03_Перечень':'Перечень документов — ПО','04_Заявление':'Заявление — ДПО','05_Заявление':'Заявление — ПО','06_Согласие':'Согласие на обработку ПД'}[name[:-5]]
   optional={'issued.blank_series','issued.blank_number','work.qualification_grade_line','work.recipient_authority','work.organization_bank_details','work.customer_bank_details','work.customer_contact','work.organization_contact','work.student_contact','students.qualification','students.student_contact','documents.document_series','documents.document_number'}
   required=[v for v in sorted(variables) if v not in optional]
   manifest.append(dict(code=code,name=title,category=cat,scenario=variant,document_type=typ,variant=variant,path=str(destpath.relative_to(root)),background=None,group=group,pdf=False,required_variables=required,variables=sorted(variables),version=1,sha256=hashlib.sha256(out.getvalue()).hexdigest(),source_archive=filename,source_name=name,source_sha256=hashlib.sha256(data).hexdigest(),original_variables=sorted(original_variables),content_layout_sha256=hashlib.sha256(re.sub(r'{{.*?}}','',ZipFile(BytesIO(data)).read('word/document.xml').decode()).encode()).hexdigest()))
(root/'manifest.json').write_text(json.dumps({'version':1,'templates':manifest},ensure_ascii=False,indent=2)+'\n')
print('Prepared',len(manifest),'working templates')
