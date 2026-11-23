"""Явные вымышленные подтверждения для сценариев прежних этапов; проверки не обходятся."""
from proficentr.admission import AdmissionRepository


def ready_for_admission(connection,storage,training_ids):
    repo=AdmissionRepository(connection,storage)
    for key in training_ids:
        review=repo.review(key)
        if review.historical:continue
        repo.save_context(key,pd_basis='contract',pd_reference='Вымышленное обращение слушателя о заключении договора; п. 5 ч. 1 ст. 6 152-ФЗ',
            pd_scope='Приём и обучение: необходимые сведения слушателя',conditions={},note='',responsible='Ответственный Тестов',day='2020-01-01',expected_signature=review.signature)
        for item in repo.review(key).items:
            if item.required and item.status!='verified':
                repo.record(key,item.key,state='verified',reason='Вымышленный оригинал / условие проверены для теста.',day='2020-01-01',
                            responsible='Ответственный Тестов',signed=True,reusable=True,education_basis='spo' if item.key=='dpo_education' else '',expected_signature=repo.review(key).signature)
        review=repo.review(key)
        assert review.ready, review.problems
